"""Motor da Máquina de Cortes.

Fluxo: obter vídeo (arquivo ou link) -> transcrever (Whisper) -> IA escolhe os
melhores trechos (Claude) -> renderizar cada corte (9:16, zoom, título, legendas).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]

PASTA_APP = Path(__file__).resolve().parent
PASTA_DOWNLOADS = PASTA_APP / "downloads"
PASTA_SAIDA = PASTA_APP / "cortes"

MODELO_CLAUDE = "claude-opus-5-5"


# ----------------------------------------------------------------------------
# Opções
# ----------------------------------------------------------------------------
@dataclass
class Opcoes:
    qtd_cortes: int = 5
    dur_min: int = 30
    dur_max: int = 75
    modelo_whisper: str = "small"
    idioma: str = "pt"
    vertical: bool = True
    legendas: bool = True
    zoom: bool = True
    titulo: bool = True
    api_key: str = ""
    instrucoes_extras: str = ""


@dataclass
class Palavra:
    inicio: float
    fim: float
    texto: str


@dataclass
class Corte:
    inicio: float
    fim: float
    titulo: str
    descricao: str
    nota: int
    palavras: list[Palavra] = field(default_factory=list)


# ----------------------------------------------------------------------------
# FFmpeg
# ----------------------------------------------------------------------------
def achar_exe(nome: str) -> str:
    achado = shutil.which(nome)
    if achado:
        return achado
    candidatos = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{nome}.exe",
    ]
    pacotes = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if pacotes.exists():
        candidatos += list(pacotes.glob(f"Gyan.FFmpeg*/**/bin/{nome}.exe"))
    for c in candidatos:
        if c.exists():
            return str(c)
    raise RuntimeError(
        f"Não encontrei o {nome}. Instale com:  winget install Gyan.FFmpeg  e abra o programa de novo."
    )


def dimensoes_video(video: Path) -> tuple[int, int]:
    saida = subprocess.run(
        [achar_exe("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    s = json.loads(saida)["streams"][0]
    return int(s["width"]), int(s["height"])


# ----------------------------------------------------------------------------
# 1. Obter o vídeo
# ----------------------------------------------------------------------------
def eh_link(fonte: str) -> bool:
    return bool(re.match(r"^https?://", fonte.strip(), re.I))


def obter_video(fonte: str, log: Log) -> Path:
    fonte = fonte.strip().strip('"')
    if not eh_link(fonte):
        p = Path(fonte)
        if not p.exists():
            raise RuntimeError(f"Arquivo não encontrado: {p}")
        return p

    import yt_dlp

    PASTA_DOWNLOADS.mkdir(exist_ok=True)
    log("Baixando vídeo do link...")

    def progresso(d):
        if d.get("status") == "finished":
            log("Download concluído, juntando áudio e vídeo...")

    opcoes = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(PASTA_DOWNLOADS / "%(title).80s [%(id)s].%(ext)s"),
        "ffmpeg_location": str(Path(achar_exe("ffmpeg")).parent),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progresso],
    }
    with yt_dlp.YoutubeDL(opcoes) as ydl:
        info = ydl.extract_info(fonte, download=True)
        baixados = info.get("requested_downloads") or []
        caminho = baixados[0]["filepath"] if baixados else ydl.prepare_filename(info)
    log(f"Vídeo salvo em: {caminho}")
    return Path(caminho)


# ----------------------------------------------------------------------------
# 2. Transcrever
# ----------------------------------------------------------------------------
def transcrever(video: Path, op: Opcoes, log: Log) -> list[dict]:
    cache = video.with_suffix(f".transcricao-{op.modelo_whisper}.json")
    if cache.exists():
        log("Usando transcrição já salva (cache).")
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel

    log(f"Carregando Whisper '{op.modelo_whisper}' (na 1ª vez ele baixa o modelo)...")
    modelo = WhisperModel(op.modelo_whisper, device="auto", compute_type="int8")
    log("Extraindo o áudio...")
    import numpy as np
    bruto = subprocess.run(
        [achar_exe("ffmpeg"), "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"], capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(bruto, dtype=np.float32)
    del bruto
    log("Transcrevendo... (isso é a parte mais demorada)")
    segs, info = modelo.transcribe(
        audio, language=op.idioma or None, word_timestamps=True, vad_filter=True,
    )
    resultado = []
    total = info.duration or 1
    ultimo_pct = -10
    for s in segs:
        resultado.append({
            "inicio": s.start, "fim": s.end, "texto": s.text.strip(),
            "palavras": [{"inicio": w.start, "fim": w.end, "texto": w.word.strip()}
                         for w in (s.words or []) if w.word.strip()],
        })
        pct = int(s.end / total * 100)
        if pct >= ultimo_pct + 10:
            ultimo_pct = pct
            log(f"  transcrição {pct}%")
    cache.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    log(f"Transcrição pronta: {len(resultado)} trechos.")
    return resultado


# ----------------------------------------------------------------------------
# 3. IA escolhe os cortes
# ----------------------------------------------------------------------------
SCHEMA_CORTES = {
    "type": "object",
    "properties": {
        "cortes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "inicio": {"type": "number", "description": "segundo de início"},
                    "fim": {"type": "number", "description": "segundo de fim"},
                    "titulo": {"type": "string", "description": "título curto e chamativo, até 8 palavras"},
                    "descricao": {"type": "string", "description": "legenda do post com 2-3 hashtags"},
                    "nota": {"type": "integer", "description": "potencial de viralizar de 1 a 10"},
                },
                "required": ["inicio", "fim", "titulo", "descricao", "nota"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cortes"],
    "additionalProperties": False,
}


def _fmt(t: float) -> str:
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:04.1f}" if h else f"{m:02d}:{s:04.1f}"


def escolher_cortes(segmentos: list[dict], op: Opcoes, log: Log) -> list[Corte]:
    import anthropic

    if not segmentos:
        raise RuntimeError("A transcrição veio vazia — o vídeo tem fala?")

    linhas = [f"[{s['inicio']:.1f}s | {_fmt(s['inicio'])}] {s['texto']}" for s in segmentos]
    transcricao = "\n".join(linhas)

    sistema = (
        "Você é um editor especialista em cortes virais para TikTok, Reels e YouTube Shorts. "
        "Você recebe a transcrição de um vídeo longo, com o segundo de início de cada fala entre colchetes, "
        "e escolhe os trechos com maior potencial de prender a atenção: histórias completas, opiniões fortes, "
        "humor, revelações, conselhos práticos, momentos polêmicos ou emocionantes. "
        "Cada corte precisa fazer sentido sozinho para quem não viu o vídeo: começa num gancho forte "
        "(sem começar no meio de uma frase) e termina quando a ideia fecha. Cortes não podem se sobrepor."
    )
    pedido = (
        f"Escolha até {op.qtd_cortes} cortes, cada um entre {op.dur_min} e {op.dur_max} segundos. "
        "Use os segundos dos colchetes para 'inicio' e 'fim'. Escreva título e descrição em português do Brasil. "
        "Ordene do melhor para o pior."
    )
    if op.instrucoes_extras.strip():
        pedido += f"\n\nInstruções extras do usuário: {op.instrucoes_extras.strip()}"

    log(f"Pedindo para a IA ({MODELO_CLAUDE}) escolher os melhores momentos...")
    cliente = anthropic.Anthropic(api_key=op.api_key.strip() or None)
    resp = cliente.beta.messages.create(
        model=MODELO_CLAUDE,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA_CORTES}},
        system=sistema,
        messages=[{
            "role": "user",
            "content": f"<transcricao>\n{transcricao}\n</transcricao>\n\n{pedido}",
        }],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("A IA recusou analisar este vídeo.")
    texto = "".join(b.text for b in resp.content if b.type == "text")
    dados = json.loads(texto)

    # todas as palavras com tempo, para alinhar os cortes certinho
    palavras = [Palavra(w["inicio"], w["fim"], w["texto"]) for s in segmentos for w in s["palavras"]]
    cortes: list[Corte] = []
    for c in dados["cortes"]:
        ini, fim = float(c["inicio"]), float(c["fim"])
        if fim <= ini:
            continue
        dentro = [w for w in palavras if w.inicio >= ini - 0.5 and w.fim <= fim + 1.5]
        if not dentro:
            continue
        ini = max(0.0, dentro[0].inicio - 0.15)
        fim = dentro[-1].fim + 0.35
        # evita sobreposição com cortes já aceitos
        if any(ini < o.fim and fim > o.inicio for o in cortes):
            continue
        cortes.append(Corte(ini, fim, c["titulo"].strip(), c["descricao"].strip(),
                            int(c["nota"]), dentro))
    log(f"A IA escolheu {len(cortes)} cortes.")
    for i, c in enumerate(cortes, 1):
        log(f"  {i}. [{_fmt(c.inicio)} → {_fmt(c.fim)}] nota {c.nota} — {c.titulo}")
    return cortes


# ----------------------------------------------------------------------------
# 4. Edição
# ----------------------------------------------------------------------------
def centro_do_rosto(video: Path, ini: float, fim: float) -> float | None:
    """Retorna a posição X (0 a 1) mediana do maior rosto no trecho."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    xs = []
    amostras = 24
    for i in range(amostras):
        t = ini + (fim - ini) * (i + 0.5) / amostras
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        escala = 640 / w
        peq = cv2.resize(frame, (640, int(h * escala)))
        cinza = cv2.cvtColor(peq, cv2.COLOR_BGR2GRAY)
        rostos = detector.detectMultiScale(cinza, 1.1, 6, minSize=(40, 40))
        if len(rostos):
            x, y, rw, rh = max(rostos, key=lambda r: r[2] * r[3])
            xs.append((x + rw / 2) / 640)
    cap.release()
    return statistics.median(xs) if len(xs) >= 3 else None


def _ass_tempo(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_esc(txt: str) -> str:
    return txt.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def gerar_ass(corte: Corte, largura: int, altura: int, op: Opcoes) -> str:
    dur = corte.fim - corte.inicio
    tam_leg = int(altura * 0.045) if altura > largura else int(altura * 0.07)
    tam_tit = int(altura * 0.032) if altura > largura else int(altura * 0.055)
    margem_leg = int(altura * 0.28) if altura > largura else int(altura * 0.10)
    margem_tit = int(altura * 0.10) if altura > largura else int(altura * 0.05)

    linhas = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {largura}", f"PlayResY: {altura}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Leg,Arial Black,{tam_leg},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(4, tam_leg // 12)},2,2,60,60,{margem_leg},1",
        f"Style: Tit,Arial,{tam_tit},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,"
        f"-1,0,0,0,100,100,0,0,3,{max(10, tam_tit // 4)},0,8,90,90,{margem_tit},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if op.titulo and corte.titulo:
        linhas.append(f"Dialogue: 1,{_ass_tempo(0)},{_ass_tempo(dur)},Tit,,0,0,0,,"
                      f"{{\\fad(250,0)}}{_ass_esc(corte.titulo.upper())}")

    if op.legendas:
        # agrupa as palavras em blocos de até 3 (quebra em pausas)
        grupos: list[list[Palavra]] = []
        for w in corte.palavras:
            if (not grupos or len(grupos[-1]) >= 3
                    or w.inicio - grupos[-1][-1].fim > 0.6):
                grupos.append([])
            grupos[-1].append(w)
        for g in grupos:
            for i, w in enumerate(g):
                ini = w.inicio - corte.inicio
                fim = (g[i + 1].inicio if i + 1 < len(g) else w.fim) - corte.inicio
                partes = []
                for j, x in enumerate(g):
                    palavra = _ass_esc(x.texto.upper())
                    if j == i:
                        partes.append(f"{{\\c&H0000FFFF&\\fscx112\\fscy112}}{palavra}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    else:
                        partes.append(palavra)
                linhas.append(f"Dialogue: 0,{_ass_tempo(ini)},{_ass_tempo(max(fim, ini + 0.05))},Leg,,0,0,0,,"
                              + " ".join(partes))
    return "\n".join(linhas) + "\n"


def _par(n: float) -> int:
    return int(n) // 2 * 2


def renderizar(video: Path, corte: Corte, destino: Path, op: Opcoes, log: Log) -> None:
    vw, vh = dimensoes_video(video)
    dur = corte.fim - corte.inicio
    filtros = []

    if op.vertical:
        W, H = 1080, 1920
        if vw / vh > 9 / 16:  # vídeo mais largo que 9:16 -> recorta nas laterais
            cw = _par(vh * 9 / 16)
            cx = centro_do_rosto(video, corte.inicio, corte.fim)
            if cx is None:
                cx = 0.5
                log("    (não achei rosto, usando o centro)")
            x = int(min(max(cx * vw - cw / 2, 0), vw - cw))
            filtros.append(f"crop={cw}:{vh}:{x}:0")
        else:
            ch = _par(vw * 16 / 9)
            filtros.append(f"crop={vw}:{min(ch, vh)}:0:(ih-{min(ch, vh)})/2")
        filtros.append(f"scale={W}:{H}")
    else:
        W = 1920 if vw >= vh else 1080
        H = _par(W * vh / vw)
        filtros.append(f"scale={W}:{H}")

    if op.zoom:
        z = f"(1+0.10*t/{dur:.2f})"  # aproximação lenta de 10% ao longo do corte
        filtros.append(f"scale=w='2*trunc({W}*{z}/2)':h='2*trunc({H}*{z}/2)':eval=frame")
        filtros.append(f"crop={W}:{H}")

    with tempfile.TemporaryDirectory() as tmp:
        if op.legendas or op.titulo:
            Path(tmp, "leg.ass").write_text(gerar_ass(corte, W, H, op), encoding="utf-8")
            filtros.append("subtitles=leg.ass")
        filtros.append("setsar=1")

        cmd = [
            achar_exe("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{corte.inicio:.3f}", "-t", f"{dur:.3f}", "-i", str(video.resolve()),
            "-vf", ",".join(filtros),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(destino.resolve()),
        ]
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg falhou:\n{r.stderr[-1500:]}")


# ----------------------------------------------------------------------------
# Tudo junto
# ----------------------------------------------------------------------------
def _nome_arquivo(txt: str) -> str:
    txt = re.sub(r'[<>:"/\\|?*\n\r\t]', "", txt).strip()
    return (txt[:60] or "corte").rstrip(". ")


def processar(fonte: str, op: Opcoes, log: Log) -> Path:
    achar_exe("ffmpeg")
    video = obter_video(fonte, log)
    segmentos = transcrever(video, op, log)
    cortes = escolher_cortes(segmentos, op, log)
    if not cortes:
        raise RuntimeError("A IA não encontrou cortes bons neste vídeo.")

    pasta = PASTA_SAIDA / _nome_arquivo(video.stem)
    pasta.mkdir(parents=True, exist_ok=True)
    resumo = []
    for i, c in enumerate(cortes, 1):
        nome = f"{i:02d} - {_nome_arquivo(c.titulo)}.mp4"
        log(f"Editando corte {i}/{len(cortes)}: {c.titulo}")
        renderizar(video, c, pasta / nome, op, log)
        resumo.append({"arquivo": nome, "titulo": c.titulo, "descricao": c.descricao,
                       "nota": c.nota, "inicio": round(c.inicio, 2), "fim": round(c.fim, 2)})
    (pasta / "cortes.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(pasta / "legendas_dos_posts.txt", "w", encoding="utf-8") as f:
        for r in resumo:
            f.write(f"{r['arquivo']}\n{r['titulo']}\n{r['descricao']}\n\n")
    log(f"Pronto! {len(cortes)} cortes salvos em: {pasta}")
    return pasta
