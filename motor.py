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
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]

if getattr(sys, "frozen", False):
    # rodando como MaquinaDeCortes.exe: dados ficam em Documentos\MaquinaCortes
    PASTA_APP = Path.home() / "Documents" / "MaquinaCortes"
    PASTA_APP.mkdir(parents=True, exist_ok=True)
else:
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
    bloco_min: int = 30  # live ao vivo: de quanto em quanto tempo sai uma leva de cortes
    layout: str = "streamer"  # "streamer" = webcam em cima e jogo embaixo; "normal" = um quadro só
    cam_regiao: list | None = None  # webcam marcada à mão: [x, y, largura, altura] em frações do quadro
    publicar_youtube: bool = False  # envia cada corte pronto para o YouTube Shorts
    yt_modo: str = "publico"  # publico | nao_listado | privado | agendado
    yt_intervalo_min: int = 20  # modo agendado: um Short a cada X minutos
    yt_nota_min: int = 7  # só publica cortes com nota da IA igual ou maior


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
    tags: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------
# FFmpeg
# ----------------------------------------------------------------------------
SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(*args, **kwargs):
    kwargs.setdefault("creationflags", SEM_JANELA)
    return subprocess.run(*args, **kwargs)


def tem_ffmpeg() -> bool:
    try:
        achar_exe("ffmpeg")
        achar_exe("ffprobe")
        return True
    except RuntimeError:
        return False


def instalar_ffmpeg(log: Log) -> None:
    """Instala o FFmpeg pelo winget (gerenciador de apps do Windows)."""
    log("Instalando o FFmpeg pelo winget (uma vez só)...")
    r = _run(["winget", "install", "-e", "--id", "Gyan.FFmpeg", "--silent",
              "--accept-package-agreements", "--accept-source-agreements"],
             capture_output=True, text=True)
    if not tem_ffmpeg():
        raise RuntimeError("Não consegui instalar o FFmpeg automaticamente.\n"
                           "Instale manualmente: abra o PowerShell e rode  winget install Gyan.FFmpeg\n\n"
                           + (r.stdout or r.stderr)[-500:])
    log("FFmpeg instalado!")


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
    saida = _run(
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
    # o tamanho entra no nome: blocos de live diferentes nunca reaproveitam a transcrição errada
    cache = video.with_suffix(f".transcricao-{op.modelo_whisper}-{video.stat().st_size}.json")
    if cache.exists():
        log("Usando transcrição já salva (cache).")
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel

    log(f"Carregando Whisper '{op.modelo_whisper}' (na 1ª vez ele baixa o modelo)...")
    modelo = WhisperModel(op.modelo_whisper, device="auto", compute_type="int8")
    log("Extraindo o áudio...")
    import numpy as np
    bruto = _run(
        [achar_exe("ffmpeg"), "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"], capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(bruto, dtype=np.float32)
    del bruto
    log("Transcrevendo... (isso é a parte mais demorada)")
    segs, info = modelo.transcribe(
        # sem filtro de voz: em live de jogo ele descartava a fala do streamer junto com o som do jogo
        audio, language=op.idioma or None, word_timestamps=True, vad_filter=False,
        condition_on_previous_text=False,
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
                    "descricao": {"type": "string", "description": "legenda do post: 1-2 frases que dão vontade "
                                  "de assistir + 3 a 5 hashtags (jogo, streamer, tema), sem #shorts"},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "5 a 10 termos de busca do YouTube (sem #)"},
                    "nota": {"type": "integer", "description": "potencial de viralizar de 1 a 10"},
                },
                "required": ["inicio", "fim", "titulo", "descricao", "nota", "tags"],
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


FORMATO_CORTES = ('{"cortes": [{"inicio": 12.3, "fim": 70.1, "titulo": "...", '
                  '"descricao": "... #hashtag #hashtag #hashtag", "nota": 8, "tags": ["termo", "termo"]}]}')


def perguntar_ia(sistema: str, conteudo: str, op: Opcoes, log: Log, schema: dict, formato: str,
                 tarefa: str) -> dict:
    """Pergunta à IA e devolve JSON: pela API (se houver chave) ou pelo Claude Code (assinatura)."""
    if op.api_key.strip() or os.environ.get("ANTHROPIC_API_KEY"):
        return _ia_pela_api(sistema, conteudo, op, log, schema, tarefa)
    return _ia_pelo_claude_code(sistema, conteudo, log, formato, tarefa)


def _ia_pela_api(sistema: str, conteudo: str, op: Opcoes, log: Log, schema: dict | None = None,
                 tarefa: str = "escolher os melhores momentos") -> dict:
    """Usa a API da Anthropic (chave do console.anthropic.com, cobrança por uso)."""
    import anthropic

    schema = schema or SCHEMA_CORTES
    log(f"Pedindo para a IA ({MODELO_CLAUDE}, via API) {tarefa}...")
    cliente = anthropic.Anthropic(api_key=op.api_key.strip() or None)
    resp = cliente.beta.messages.create(
        model=MODELO_CLAUDE,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        system=sistema,
        messages=[{"role": "user", "content": conteudo}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("A IA recusou analisar este vídeo.")
    return json.loads("".join(b.text for b in resp.content if b.type == "text"))


def achar_claude_code() -> str | None:
    achado = shutil.which("claude")
    if achado:
        return achado
    local = Path.home() / ".local" / "bin" / "claude.exe"
    if local.exists():
        return str(local)
    # cópia que vem junto com o app Claude para desktop (a pasta muda a cada atualização)
    dados = Path(os.environ.get("LOCALAPPDATA", "")) / "Claude-Data"
    copias = sorted(dados.glob("*/claude-code/*/*/claude.exe"), key=lambda p: p.stat().st_mtime)
    return str(copias[-1]) if copias else None


def _ia_pelo_claude_code(sistema: str, conteudo: str, log: Log, formato: str = FORMATO_CORTES,
                         tarefa: str = "escolher os melhores momentos") -> dict:
    """Usa o Claude Code logado na sua assinatura (Pro/Max), sem chave de API."""
    exe = achar_claude_code()
    if not exe:
        raise RuntimeError(
            "Sem chave de API e sem Claude Code instalado.\n"
            "Coloque uma chave da API, ou instale o Claude Code e faça login com sua conta Pro "
            "(veja o README)."
        )
    log(f"Pedindo para o Claude (pela sua assinatura, via Claude Code) {tarefa}...")
    instrucao = (
        f"{sistema}\n\nResponda SOMENTE com um JSON válido, sem texto antes ou depois, neste formato:\n{formato}"
    )
    # ambiente limpo: garante que ele use o seu login, e não variáveis de outro programa
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k.startswith("ANTHROPIC"))}
    r = _run(
        [exe, "-p", instrucao, "--output-format", "json"],
        input=conteudo, capture_output=True, text=True, encoding="utf-8", timeout=900, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise RuntimeError(f"O Claude Code falhou (está logado? rode 'claude' uma vez):\n{(r.stderr or r.stdout)[-800:]}")
    resposta = json.loads(r.stdout).get("result", "")
    achado = re.search(r"\{.*\}", resposta, re.S)
    if not achado:
        raise RuntimeError(f"Resposta inesperada do Claude Code:\n{resposta[:500]}")
    return json.loads(achado.group(0))


def _licoes_do_canal() -> str:
    try:
        import aprendizado
        return aprendizado.texto_para_prompt()
    except Exception:
        return ""


def escolher_cortes(segmentos: list[dict], op: Opcoes, log: Log) -> list[Corte]:
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
        "(sem começar no meio de uma frase) e termina quando a ideia fecha. Cortes não podem se sobrepor.\n\n"
        "O que faz um Short performar (use como critério):\n"
        "- Os 2 primeiros segundos decidem tudo: comece na frase mais intrigante, engraçada ou surpreendente, "
        "não na preparação. Evite começar com 'então', 'bom', silêncio ou explicação.\n"
        "- Um momento só por corte, com começo, clímax e fim; termine logo depois da punchline ou da reação, "
        "sem 'rabo' depois do ponto alto.\n"
        "- Reações fortes (riso, raiva, susto, indignação), frases citáveis e situações absurdas do jogo "
        "rendem mais que conversa morna ou leitura de chat.\n"
        "- Prefira cortes mais curtos e densos a cortes longos com partes paradas.\n"
        "- Título: até ~60 caracteres, curioso e fiel ao que acontece (sem mentir), em linguagem de internet; "
        "pode usar 1 emoji. Não comece com o nome do jogo.\n"
        "- 'nota' é sua aposta honesta de desempenho (1-10); reserve 9-10 para momentos realmente fortes."
    )
    licoes = _licoes_do_canal()
    if licoes:
        sistema += "\n\n" + licoes
    pedido = (
        f"Escolha até {op.qtd_cortes} cortes, cada um entre {op.dur_min} e {op.dur_max} segundos. "
        "Use os segundos dos colchetes para 'inicio' e 'fim'. Escreva título e descrição em português do Brasil. "
        "Na descrição: 1 ou 2 frases que deem vontade de assistir, seguidas de 3 a 5 hashtags relevantes "
        "(o jogo, o nome do streamer se aparecer na fala ou no contexto, o tema do momento); não use #shorts. "
        "Em 'tags': 5 a 10 termos que as pessoas buscariam no YouTube para achar esse corte. "
        "Ordene do melhor para o pior."
    )
    if op.instrucoes_extras.strip():
        pedido += f"\n\nInstruções extras do usuário: {op.instrucoes_extras.strip()}"

    conteudo = f"<transcricao>\n{transcricao}\n</transcricao>\n\n{pedido}"
    dados = perguntar_ia(sistema, conteudo, op, log, SCHEMA_CORTES, FORMATO_CORTES, "escolher os melhores momentos")

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
                            int(c["nota"]), dentro, tags=list(c.get("tags") or [])[:15]))
    log(f"A IA escolheu {len(cortes)} cortes.")
    for i, c in enumerate(cortes, 1):
        log(f"  {i}. [{_fmt(c.inicio)} → {_fmt(c.fim)}] nota {c.nota} — {c.titulo}")
    return cortes


# ----------------------------------------------------------------------------
# 4. Edição
# ----------------------------------------------------------------------------
CAM_H, GAME_H = 768, 1152  # layout streamer: webcam em cima (1080x768), jogo embaixo (1080x1152)


def detectar_rosto(video: Path, ini: float, fim: float) -> tuple[float, float, float] | None:
    """Mediana do maior rosto no trecho: (centro x, centro y, largura do rosto), em pixels do vídeo."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    achados = []
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
        rostos = detector.detectMultiScale(cinza, 1.1, 6, minSize=(30, 30))
        if len(rostos):
            x, y, rw, rh = max(rostos, key=lambda r: r[2] * r[3])
            achados.append(((x + rw / 2) / escala, (y + rh / 2) / escala, rw / escala))
    cap.release()
    if len(achados) < 3:
        return None
    return tuple(statistics.median(a[i] for a in achados) for i in range(3))


def centro_do_rosto(video: Path, ini: float, fim: float) -> float | None:
    """Posição X (0 a 1) do rosto, usada no layout normal."""
    r = detectar_rosto(video, ini, fim)
    if not r:
        return None
    vw, _ = dimensoes_video(video)
    return r[0] / vw


def capturar_quadro(fonte: str, destino: Path) -> Path:
    """Salva uma imagem (960px de largura) de um vídeo ou link, para marcar a webcam."""
    fonte = fonte.strip().strip('"')
    if eh_link(fonte):
        info = info_do_link(fonte, "best[height<=1080]/best")
        cab = "".join(f"{k}: {v}\r\n" for k, v in (info.get("http_headers") or {}).items())
        entrada = ["-headers", cab] if cab else []
        if not info.get("is_live"):
            entrada += ["-ss", "120"]
        entrada += ["-i", info["url"]]
    else:
        dur = _duracao(Path(fonte))
        entrada = ["-ss", f"{min(120.0, dur / 3):.1f}", "-i", fonte]
    r = _run([achar_exe("ffmpeg"), "-v", "error", "-y", *entrada, "-frames:v", "1",
                        "-vf", "scale=960:-2", str(destino)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not destino.exists():
        raise RuntimeError(f"Não consegui pegar uma imagem do vídeo:\n{r.stderr[-500:]}")
    return destino


def _ass_tempo(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_esc(txt: str) -> str:
    return txt.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


TAM_FAIXA = 62  # tamanho da letra do título na faixa vermelha


def _quebrar(txt: str, max_chars: int = 24) -> list[str]:
    """Quebra o título em linhas de tamanho parecido (evita uma palavra sozinha na última linha)."""
    palavras = txt.split()
    n = max(1, -(-len(txt) // max_chars))
    while True:
        alvo = len(txt) / n
        linhas, atual = [], ""
        for palavra in palavras:
            if atual and len(atual) + 1 + len(palavra) > max(alvo + 4, 1) and len(linhas) < n - 1:
                linhas.append(atual)
                atual = palavra
            else:
                atual = f"{atual} {palavra}".strip()
        linhas.append(atual)
        if all(len(l) <= max_chars for l in linhas) or n >= 3:
            return linhas[:3]
        n += 1


def _altura_faixa(titulo: str) -> int:
    """Altura total da faixa do título (incluindo a sombra de 10px embaixo)."""
    return len(_quebrar(titulo.upper())) * int(TAM_FAIXA * 1.18) + 2 * 24 + 10


def _faixa_vermelha(titulo: str, largura: int, y0: int, dur: float) -> list[str]:
    """Faixa vermelha na largura toda, com detalhe amarelo em cima, sombra vinho embaixo e texto branco."""
    linhas_tit = _quebrar(titulo.upper())
    alt = _altura_faixa(titulo) - 10
    fim = _ass_tempo(dur)

    def ret(y, h, cor_bgr, camada, extra=""):
        return (f"Dialogue: {camada},{_ass_tempo(0)},{fim},Desenho,,0,0,0,,"
                f"{{\\an7\\pos(0,{y})\\1c&H{cor_bgr}&\\fad(150,0){extra}\\p1}}"
                f"m 0 0 l {largura} 0 l {largura} {h} l 0 {h}{{\\p0}}")

    texto = "\\N".join(_ass_esc(l) for l in linhas_tit)
    return [
        ret(y0 + alt, 10, "00006E", 2),            # sombra vinho embaixo
        ret(y0, alt, "1C1CD9", 3),                 # faixa vermelha
        ret(y0, 8, "00D2FF", 4),                   # detalhe amarelo em cima
        ret(y0 + alt - 4, 4, "0E0E9A", 4),         # filete escuro na base
        f"Dialogue: 5,{_ass_tempo(0)},{fim},TitV,,0,0,0,,"
        f"{{\\an5\\pos({largura // 2},{y0 + alt // 2 + 3})\\fad(150,0)"
        f"\\fscx80\\fscy80\\t(0,220,\\fscx100\\fscy100)}}{texto}",
    ]


def gerar_ass(corte: Corte, largura: int, altura: int, op: Opcoes, margem_tit: int | None = None,
              titulo_vermelho: bool = False, margem_leg: int | None = None) -> str:
    dur = corte.fim - corte.inicio
    tam_leg = int(altura * 0.045) if altura > largura else int(altura * 0.07)
    tam_tit = int(altura * 0.032) if altura > largura else int(altura * 0.055)
    if margem_leg is None:
        margem_leg = int(altura * 0.28) if altura > largura else int(altura * 0.10)
    if margem_tit is None:
        margem_tit = int(altura * 0.10) if altura > largura else int(altura * 0.05)
    # caixa do título: no estilo 3 do ASS a cor da caixa é a "OutlineColour" (formato &HAABBGGRR)
    if titulo_vermelho:
        cor_texto, cor_caixa = "&H00FFFFFF", "&H001A1AE0"  # texto branco, caixa vermelha
    else:
        cor_texto, cor_caixa = "&H00000000", "&H00FFFFFF"  # texto preto, caixa branca

    linhas = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {largura}", f"PlayResY: {altura}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Leg,Arial Black,{tam_leg},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(4, tam_leg // 12)},2,2,60,60,{margem_leg},1",
        f"Style: Tit,Arial,{tam_tit},{cor_texto},{cor_texto},{cor_caixa},{cor_caixa},"
        f"-1,0,0,0,100,100,0,0,3,{max(10, tam_tit // 4)},0,8,90,90,{margem_tit},1",
        f"Style: TitV,Arial Black,{TAM_FAIXA},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,"
        "-1,0,0,0,100,100,0,0,1,4,3,5,40,40,0,1",
        "Style: Desenho,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if op.titulo and corte.titulo and titulo_vermelho:
        linhas += _faixa_vermelha(corte.titulo, largura, margem_tit, dur)
    elif op.titulo and corte.titulo:
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


def _caixa_na_proporcao(cx: float, cy: float, w: float, h: float, prop: float,
                        vw: int, vh: int) -> tuple[int, int, int, int]:
    """Recorta (w,h) centrado em (cx,cy) na proporção largura/altura `prop`, sem sair do quadro."""
    if w / h > prop:
        w = h * prop
    else:
        h = w / prop
    w, h = _par(min(w, vw)), _par(min(h, vh))
    x = int(min(max(cx - w / 2, 0), vw - w))
    y = int(min(max(cy - h / 2, 0), vh - h))
    return w, h, x, y


def _regiao_webcam(video: Path, corte: Corte, op: Opcoes, vw: int, vh: int, log: Log):
    """Retorna a caixa (w,h,x,y) da webcam recortada e a faixa X (x0,x1) que a webcam ocupa no quadro."""
    prop = 1080 / CAM_H
    if op.cam_regiao:
        rx, ry, rw, rh = op.cam_regiao
        x0, y0, w, h = rx * vw, ry * vh, rw * vw, rh * vh
        return _caixa_na_proporcao(x0 + w / 2, y0 + h / 2, w, h, prop, vw, vh), (x0, x0 + w)
    rosto = detectar_rosto(video, corte.inicio, corte.fim)
    if not rosto:
        log("    (não achei a webcam, usando o layout normal)")
        return None, None
    cx, cy, fw = rosto
    largura = fw * 1.8  # só o rosto: apertado para não pegar a borda da webcam nem alertas
    caixa = _caixa_na_proporcao(cx, cy + fw * 0.05, largura, largura / prop, prop, vw, vh)
    return caixa, (cx - fw * 2.2, cx + fw * 2.2)  # faixa aproximada da webcam inteira


def _x_do_jogo(gw: int, vw: int, faixa_cam: tuple[float, float]) -> int:
    """Escolhe o recorte do jogo que mostra o mínimo possível da webcam (empate: o do meio)."""
    candidatos = [(vw - gw) // 2, 0, vw - gw]

    def sobra(x):
        return max(0.0, min(x + gw, faixa_cam[1]) - max(x, faixa_cam[0]))

    return min(candidatos, key=lambda x: (round(sobra(x)), candidatos.index(x)))


def renderizar(video: Path, corte: Corte, destino: Path, op: Opcoes, log: Log) -> None:
    vw, vh = dimensoes_video(video)
    dur = corte.fim - corte.inicio
    zoom = f"(1+0.10*t/{dur:.2f})"  # aproximação lenta de 10% ao longo do corte

    def com_zoom(W, H):
        return [f"scale=w='2*trunc({W}*{zoom}/2)':h='2*trunc({H}*{zoom}/2)':eval=frame", f"crop={W}:{H}"]

    margem_tit = None
    margem_leg = None
    grafo = None
    filtros = []

    if op.vertical and op.layout == "streamer" and vw > vh:
        cam, faixa = _regiao_webcam(video, corte, op, vw, vh, log)
        if cam:
            W, H = 1080, CAM_H + GAME_H
            cw, ch, cx, cy = cam
            # jogo inteiro (16:9, sem cortar nada), centralizado logo abaixo da faixa do título,
            # em cima de um fundo desfocado do próprio jogo
            jogo_h = _par(1080 * vh / vw)
            faixa_h = _altura_faixa(corte.titulo) if op.titulo and corte.titulo else 0
            gy = faixa_h if faixa_h else (GAME_H - jogo_h) // 2
            cam_zoom = ",".join(com_zoom(1080, CAM_H)) + "," if op.zoom else ""
            grafo = (f"[0:v]split=3[a][b][c];"
                     f"[a]crop={cw}:{ch}:{cx}:{cy},scale=1080:{CAM_H},{cam_zoom}setsar=1[cam];"
                     f"[b]scale=1080:{GAME_H}:force_original_aspect_ratio=increase,crop=1080:{GAME_H},"
                     f"boxblur=25:2,eq=brightness=-0.12,setsar=1[fundo];"
                     f"[c]scale=1080:{jogo_h},setsar=1[jogo];"
                     f"[fundo][jogo]overlay=0:{gy}[baixo];"
                     f"[cam][baixo]vstack=inputs=2")
            margem_tit = CAM_H  # faixa do título encostada embaixo da webcam
            margem_leg = max(40, (GAME_H - gy - jogo_h) // 2 - 40)  # legenda no espaço embaixo do jogo

    if grafo is None:
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
            filtros += com_zoom(W, H)

    with tempfile.TemporaryDirectory() as tmp:
        if op.legendas or op.titulo:
            ass = gerar_ass(corte, W, H, op, margem_tit, titulo_vermelho=grafo is not None, margem_leg=margem_leg)
            Path(tmp, "leg.ass").write_text(ass, encoding="utf-8")
            filtros.append("subtitles=leg.ass")
        filtros.append("setsar=1")

        if grafo:
            video_args = ["-filter_complex", f"{grafo}[st];[st]{','.join(filtros)}[v]",
                          "-map", "[v]", "-map", "0:a?"]
        else:
            video_args = ["-vf", ",".join(filtros)]
        cmd = [
            achar_exe("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{corte.inicio:.3f}", "-t", f"{dur:.3f}", "-i", str(video.resolve()),
            *video_args,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(destino.resolve()),
        ]
        r = _run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg falhou:\n{r.stderr[-1500:]}")


# ----------------------------------------------------------------------------
# Tudo junto
# ----------------------------------------------------------------------------
def _nome_arquivo(txt: str) -> str:
    txt = re.sub(r'[<>:"/\\|?*\n\r\t]', "", txt).strip()
    return (txt[:60] or "corte").rstrip(". ")


def alterar_cortes_json(pasta: Path, mudanca: Callable[[list], None]) -> list:
    """Relê o cortes.json, aplica a mudança e grava (com trava), para duas janelas não apagarem o trabalho uma da outra."""
    arq = pasta / "cortes.json"
    trava = pasta / ".cortes.lock"
    for _ in range(200):
        try:
            fd = os.open(str(trava), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() - trava.stat().st_mtime > 30:  # trava esquecida
                trava.unlink(missing_ok=True)
            time.sleep(0.05)
    else:
        fd = None
    try:
        dados = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else []
        mudanca(dados)
        tmp = arq.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, arq)
        return dados
    finally:
        if fd is not None:
            os.close(fd)
            trava.unlink(missing_ok=True)


def cortar_video(video: Path, pasta: Path, op: Opcoes, log: Log, prefixo: str = "", fonte: str = "") -> int:
    """Transcreve, escolhe e edita os cortes de um arquivo. Retorna quantos cortes saíram."""
    segmentos = transcrever(video, op, log)
    if not segmentos:
        log("  (sem fala neste trecho, pulando)")
        return 0
    cortes = escolher_cortes(segmentos, op, log)
    pasta.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(cortes, 1):
        nome = f"{prefixo}{i:02d} - {_nome_arquivo(c.titulo)}.mp4"
        log(f"Editando corte {i}/{len(cortes)}: {c.titulo}")
        renderizar(video, c, pasta / nome, op, log)
        r = {"arquivo": nome, "titulo": c.titulo, "descricao": c.descricao, "tags": c.tags, "fonte": fonte,
             "fala": " ".join(w.texto for w in c.palavras)[:2000], "duracao": round(c.fim - c.inicio, 1),
             "nota": c.nota, "origem": video.name, "inicio": round(c.inicio, 2), "fim": round(c.fim, 2)}
        if op.publicar_youtube:
            if c.nota >= op.yt_nota_min:
                try:
                    import publicador
                    log(f"  Publicando no YouTube Shorts ({op.yt_modo})...")
                    r["youtube"] = publicador.publicar(pasta / nome, c.titulo, c.descricao,
                                                       op.yt_modo, op.yt_intervalo_min, log,
                                                       tags=c.tags, fonte=fonte)
                    publicador.registrar(pasta / nome, c.titulo, r["youtube"])
                    log(f"  ✅ No YouTube: {r['youtube']}")
                except Exception as e:
                    log(f"  ⚠ Não consegui publicar no YouTube: {e}")
            else:
                log(f"  (nota {c.nota} abaixo da mínima {op.yt_nota_min}: não publicado)")
        alterar_cortes_json(pasta, lambda dados: dados.append(r))
        with open(pasta / "legendas_dos_posts.txt", "a", encoding="utf-8") as f:
            f.write(f"{r['arquivo']}\n{r['titulo']}\n{r['descricao']}\n\n")
    return len(cortes)


def checar_caminho() -> None:
    """O Windows limita caminhos a 260 caracteres; avisa antes de perder tempo transcrevendo."""
    if os.name == "nt" and len(str(PASTA_APP)) > 120:
        raise RuntimeError(
            f"A pasta do programa tem um caminho muito longo ({len(str(PASTA_APP))} caracteres) e o Windows "
            "não consegue salvar os arquivos.\nMova a pasta para um lugar com caminho curto, "
            "por exemplo C:\\MaquinaCortes ou Documentos\\MaquinaCortes."
        )


def _aprender_se_preciso(op: Opcoes, log: Log) -> None:
    """Atualiza as lições do canal no máximo a cada 12 h (só com o YouTube conectado)."""
    try:
        import aprendizado
        import publicador
        if not publicador.conectado():
            return
        arq = aprendizado.ARQ_LICOES
        if arq.exists() and time.time() - arq.stat().st_mtime < 12 * 3600:
            return
        log("Atualizando o que a IA aprendeu com o seu canal...")
        aprendizado.atualizar_desempenho(log)
        aprendizado.gerar_licoes(op, log)
    except Exception as e:
        log(f"  (não consegui atualizar as lições do canal: {e})")


def processar(fonte: str, op: Opcoes, log: Log, parar: threading.Event | None = None) -> Path:
    checar_caminho()
    achar_exe("ffmpeg")
    _aprender_se_preciso(op, log)
    parar = parar or threading.Event()
    if eh_link(fonte):
        info = info_do_link(fonte)
        if info.get("is_live"):
            return processar_ao_vivo(fonte, info, op, log, parar)
    video = obter_video(fonte, log)
    pasta = PASTA_SAIDA / _nome_arquivo(video.stem)
    if (pasta / "legendas_dos_posts.txt").exists():
        (pasta / "legendas_dos_posts.txt").unlink()
        (pasta / "cortes.json").unlink(missing_ok=True)
    n = cortar_video(video, pasta, op, log, fonte=fonte.strip() if eh_link(fonte) else "")
    if not n:
        raise RuntimeError("A IA não encontrou cortes bons neste vídeo.")
    log(f"Pronto! {n} cortes salvos em: {pasta}")
    return pasta


# ----------------------------------------------------------------------------
# Live ao vivo: grava em blocos e corta cada bloco assim que ele fecha
# ----------------------------------------------------------------------------
def info_do_link(url: str, formato: str = "best") -> dict:
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                           "format": formato}) as ydl:
        return ydl.extract_info(url, download=False)


def _duracao(arq: Path) -> float:
    try:
        saida = _run(
            [achar_exe("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(arq)], capture_output=True, text=True,
        ).stdout.strip()
        return float(saida)
    except Exception:
        return 0.0


def _iniciar_gravacao(url_live: str, pasta_blocos: Path, rodada: int, op: Opcoes, log: Log,
                      entrada: list[str] | None = None) -> subprocess.Popen:
    if entrada is None:
        info = info_do_link(url_live, "best[height<=1080][protocol^=m3u8]/best[height<=1080]/best")
        cabecalhos = "".join(f"{k}: {v}\r\n" for k, v in (info.get("http_headers") or {}).items())
        entrada = (["-headers", cabecalhos] if cabecalhos else []) + ["-i", info["url"]]
    cmd = [achar_exe("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *entrada,
           "-c", "copy", "-f", "segment", "-segment_time", str(op.bloco_min * 60),
           "-reset_timestamps", "1", str(pasta_blocos / f"r{rodada:02d}_bloco_%03d.ts")]
    log(f"Gravando a live em blocos de {op.bloco_min} min...")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def processar_ao_vivo(url: str, info: dict, op: Opcoes, log: Log, parar: threading.Event,
                      entrada_teste: list[str] | None = None) -> Path:
    titulo = _nome_arquivo(re.sub(r"\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", info.get("title") or "live"))
    titulo = titulo[:40].rstrip(". ")
    inicio_live = time.strftime("%d-%m %Hh%M")
    pasta = PASTA_SAIDA / f"{titulo} (ao vivo {inicio_live})"
    pasta_blocos = PASTA_DOWNLOADS / f"live-{inicio_live.replace(' ', '_')}"
    pasta_blocos.mkdir(parents=True, exist_ok=True)
    log(f"🔴 Live detectada: {info.get('title')}")
    log(f"Cada {op.bloco_min} min de live vira um bloco; os cortes de cada bloco saem assim que ele fecha.")
    log("Clique em 'Parar live' quando quiser encerrar (o último bloco ainda é cortado).")

    rodada = 1
    gravador = _iniciar_gravacao(url, pasta_blocos, rodada, op, log, entrada_teste)
    feitos: set[str] = set()
    total = 0
    n_bloco = 0

    def pendentes(gravando: bool) -> list[Path]:
        arqs = sorted(pasta_blocos.glob("r*_bloco_*.ts"))
        arqs = [a for a in arqs if a.name not in feitos]
        if gravando:  # o arquivo mais novo da rodada atual ainda está sendo escrito
            atuais = [a for a in arqs if a.name.startswith(f"r{rodada:02d}_")]
            if atuais:
                arqs.remove(atuais[-1])
        return arqs

    while True:
        gravando = gravador.poll() is None
        if parar.is_set() and gravando:
            log("Parando a gravação...")
            try:
                gravador.stdin.write("q")
                gravador.stdin.flush()
                gravador.wait(timeout=20)
            except Exception:
                gravador.kill()
            gravando = False

        for bloco in pendentes(gravando):
            feitos.add(bloco.name)
            dur = _duracao(bloco)
            if dur < 30:
                continue
            n_bloco += 1
            log(f"— Bloco {n_bloco} fechado ({dur / 60:.0f} min). Cortando...")
            try:
                total += cortar_video(bloco, pasta, op, log, prefixo=f"b{n_bloco:02d}-", fonte=url)
                bloco.unlink(missing_ok=True)  # bloco já cortado: libera espaço (~3 GB por hora de live)
            except Exception as e:
                log(f"  Erro no bloco {n_bloco}: {e}")
            log(f"— Bloco {n_bloco} pronto. Total até agora: {total} cortes em {pasta}")

        if not gravando:
            if parar.is_set() or entrada_teste is not None:
                break
            # a gravação caiu: a live acabou ou o link expirou
            erro = (gravador.stderr.read() or "").strip() if gravador.stderr else ""
            try:
                ainda_ao_vivo = bool(info_do_link(url).get("is_live"))
            except Exception:
                ainda_ao_vivo = False
            if not ainda_ao_vivo:
                log("A live terminou.")
                break
            if erro:
                log(f"  (conexão caiu: {erro[-200:]})")
            log("Reconectando na live...")
            rodada += 1
            gravador = _iniciar_gravacao(url, pasta_blocos, rodada, op, log)
            continue

        parar.wait(5)

    log(f"Pronto! {total} cortes da live salvos em: {pasta}")
    return pasta
