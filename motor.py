"""Motor da Máquina de Cortes.

Fluxo: obter vídeo (arquivo ou link) -> transcrever (Whisper) -> IA escolhe os
melhores trechos (Claude) -> renderizar cada corte (9:16, zoom, título, legendas).
"""
from __future__ import annotations

import json
import os
import queue
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
    dur_min: int = 15
    dur_max: int = 35  # Short curto é visto até o fim e repetido; nunca passe de 59 s (direitos autorais)
    modelo_whisper: str = "small"
    idioma: str = "pt"
    vertical: bool = True
    legendas: bool = True
    zoom: bool = True
    titulo: bool = True
    cortar_pausas: bool = True  # tira silêncios entre as falas (corte seco), menos onde o jogo faz barulho
    api_key: str = ""
    instrucoes_extras: str = ""
    bloco_min: int = 30  # live ao vivo: de quanto em quanto tempo sai uma leva de cortes
    layout: str = "streamer"  # "streamer" = webcam em cima e jogo embaixo; "normal" = um quadro só
    cam_regiao: list | None = None  # webcam marcada à mão: [x, y, largura, altura] em frações do quadro
    publicar_youtube: bool = False  # envia cada corte pronto para o YouTube Shorts
    yt_modo: str = "publico"  # publico | nao_listado | privado | agendado
    yt_intervalo_min: int = 20  # modo agendado: um Short a cada X minutos
    yt_nota_min: int = 7  # só publica cortes com nota da IA igual ou maior
    revisar_antes: bool = True  # publicar só depois de aprovar na tela de revisão


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
    pico: float | None = None  # segundo (no vídeo original) do ponto alto: reação, grito, punchline


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
    # --source winget: sem isso o winget também consulta a Microsoft Store (msstore),
    # e se ela não responder a instalação inteira falha
    r = _run(["winget", "install", "-e", "--id", "Gyan.FFmpeg", "--source", "winget", "--silent",
              "--accept-package-agreements", "--accept-source-agreements"],
             capture_output=True, text=True)
    if not tem_ffmpeg():
        raise RuntimeError("Não consegui instalar o FFmpeg automaticamente.\n"
                           "Instale manualmente: abra o PowerShell e rode  "
                           "winget install Gyan.FFmpeg --source winget\n\n"
                           + (r.stdout or r.stderr)[-500:])
    log("FFmpeg instalado!")


def _runtime_js() -> dict:
    """O YouTube exige um runtime de JavaScript (Deno) para o yt-dlp ver todos os formatos.
    Sem ele, só sobram formatos separados de vídeo e áudio e pedidos como "best" falham."""
    deno = shutil.which("deno")
    if not deno:
        winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
        candidatos = [winget / "Links" / "deno.exe", *winget.glob("Packages/DenoLand.Deno*/deno.exe")]
        deno = next((str(c) for c in candidatos if c.exists()), None)
    return {"js_runtimes": {"deno": {"path": deno}}} if deno else {}


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
        f"Não encontrei o {nome}. Instale com:  winget install Gyan.FFmpeg --source winget  "
        "e abra o programa de novo."
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
        **_runtime_js(),
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
# frases que o Whisper "inventa" em silêncio ou música (vêm das legendas com que ele foi treinado)
ALUCINACOES = ("amara.org", "legendas pela comunidade", "legendado por", "legendas por", "tradução e legendas",
               "transcrição por", "obrigado por assistir", "obrigada por assistir", "inscreva-se no canal",
               "não se esqueça de se inscrever", "deixe seu like", "subtítulos realizados", "sous-titres",
               "thanks for watching", "thank you for watching", "www.", ".com.br")


def eh_alucinacao(texto: str) -> bool:
    """Trecho curto que é só uma dessas frases típicas (fala longa que contém a frase é mantida)."""
    t = re.sub(r"[^\w\s.\-]", "", texto.lower()).strip()
    return not re.sub(r"[\W_]", "", t) or (len(t) <= 60 and any(a in t for a in ALUCINACOES))


def _limpar_segmentos(segmentos: list[dict]) -> list[dict]:
    return [s for s in segmentos if not eh_alucinacao(s["texto"])]


def transcrever(video: Path, op: Opcoes, log: Log) -> list[dict]:
    # o tamanho entra no nome: blocos de live diferentes nunca reaproveitam a transcrição errada
    cache = video.with_suffix(f".transcricao-{op.modelo_whisper}-{video.stat().st_size}.json")
    if cache.exists():
        log("Usando transcrição já salva (cache).")
        return _limpar_segmentos(json.loads(cache.read_text(encoding="utf-8")))

    modelo = _carregar_whisper(op.modelo_whisper, log)
    log("Transcrevendo... (isso é a parte mais demorada)")
    progresso = {"total": _duracao(video) or 1, "pct": -10}
    resultado: list[dict] = []
    descartados, inicio = 0, 0.0
    while True:  # em partes: o áudio inteiro de uma VOD longa ocupava GBs de memória
        audio = _audio_16k(video, inicio, PARTE_TRANSCRICAO_S)
        if not len(audio):
            break
        while True:
            try:
                segs, desc = _transcrever_audio(modelo, audio, op, log, inicio, progresso)
                break
            except RuntimeError as e:
                # placa NVIDIA sem as bibliotecas CUDA (cublas/cudnn): o erro só aparece ao transcrever
                if _DISPOSITIVO_WHISPER[0] == "cpu" or not re.search(r"cublas|cudnn|cuda", str(e), re.I):
                    raise
                log("  (a placa de vídeo não tem as bibliotecas CUDA: transcrevendo pelo processador)")
                _DISPOSITIVO_WHISPER[0] = "cpu"
                modelo = _carregar_whisper(op.modelo_whisper, log)
        ultima_parte = len(audio) < (PARTE_TRANSCRICAO_S - 1) * 16000
        avanco = PARTE_TRANSCRICAO_S
        if not ultima_parte and len(segs) > 1 and segs[-1]["inicio"] - inicio > PARTE_TRANSCRICAO_S / 2:
            # a última frase pode ter sido cortada no meio pela divisão: ela é refeita no começo da próxima parte
            avanco = segs.pop()["inicio"] - inicio
        resultado += segs
        descartados += desc
        del audio
        if ultima_parte:
            break
        inicio += avanco
    cache.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    log(f"Transcrição pronta: {len(resultado)} trechos"
        + (f" ({descartados} descartados: silêncio/música que o Whisper confundiu com fala)." if descartados else "."))
    return resultado


_DISPOSITIVO_WHISPER = ["auto"]  # vira "cpu" na 1ª vez que a GPU falhar, para não tentar de novo


_WHISPER_CARREGADO: dict = {}  # o último modelo usado fica na memória (na live, um por bloco era lento)


def _carregar_whisper(nome: str, log: Log):
    chave = (nome, _DISPOSITIVO_WHISPER[0])
    if chave not in _WHISPER_CARREGADO:
        from faster_whisper import WhisperModel
        log(f"Carregando Whisper '{nome}' (na 1ª vez ele baixa o modelo)...")
        _WHISPER_CARREGADO.clear()  # guarda só um: modelos grandes ocupam GBs
        _WHISPER_CARREGADO[chave] = WhisperModel(nome, device=chave[1], compute_type="int8")
    return _WHISPER_CARREGADO[chave]


PARTE_TRANSCRICAO_S = 20 * 60  # 20 min de áudio = ~77 MB na memória


def _audio_16k(video: Path, inicio: float, duracao: float):
    import numpy as np
    bruto = _run(
        [achar_exe("ffmpeg"), "-v", "error", "-ss", f"{inicio:.3f}", "-t", f"{duracao:.3f}", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True, check=True,
    ).stdout
    return np.frombuffer(bruto, dtype=np.float32)


def _transcrever_audio(modelo, audio, op: Opcoes, log: Log, inicio: float = 0.0,
                       progresso: dict | None = None) -> tuple[list[dict], int]:
    """Transcreve um pedaço de áudio; os tempos já saem somados ao `inicio` do pedaço no vídeo."""
    segs, info = modelo.transcribe(
        # sem filtro de voz: em live de jogo ele descartava a fala do streamer junto com o som do jogo
        audio, language=op.idioma or None, word_timestamps=True, vad_filter=False,
        condition_on_previous_text=False,
    )
    resultado = []
    progresso = progresso if progresso is not None else {"total": info.duration or 1, "pct": -10}
    descartados = 0
    for s in segs:
        # mesma regra do Whisper original: provável silêncio e texto com pouca confiança = inventado;
        # compressão alta = a mesma frase repetida em loop
        if (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0) or s.compression_ratio > 2.4 \
                or eh_alucinacao(s.text):
            descartados += 1
            continue
        resultado.append({
            "inicio": inicio + s.start, "fim": inicio + s.end, "texto": s.text.strip(),
            "palavras": [{"inicio": inicio + w.start, "fim": inicio + w.end, "texto": w.word.strip()}
                         for w in (s.words or []) if w.word.strip()],
        })
        pct = min(100, int((inicio + s.end) / progresso["total"] * 100))
        if pct >= progresso["pct"] + 10:
            progresso["pct"] = pct
            log(f"  transcrição {pct}%")
    return resultado, descartados


def picos_de_audio(video: Path, log: Log) -> list[dict]:
    """Momentos em que o volume dispara bem acima do normal do vídeo (grito, risada, susto, explosão).
    Devolve [{"inicio", "fim", "db"}], com db = quanto acima do volume típico."""
    cache = video.with_suffix(f".picos-{video.stat().st_size}.json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    import numpy as np

    log("Medindo o volume do áudio (gritos, risadas, sustos)...")
    taxa, janela = 8000, 4000  # janelas de 0,5 s; 8 kHz basta para medir volume
    proc = subprocess.Popen(
        [achar_exe("ffmpeg"), "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(taxa),
         "-f", "s16le", "-"], stdout=subprocess.PIPE, creationflags=SEM_JANELA)
    niveis = []
    while True:  # lê aos pedaços: não guarda o áudio inteiro na memória
        bloco = proc.stdout.read(janela * 2 * 120)
        if not bloco:
            break
        a = np.frombuffer(bloco[:len(bloco) // 2 * 2], dtype=np.int16).astype(np.float32)
        for i in range(0, len(a), janela):
            rms = float(np.sqrt(np.mean(a[i:i + janela] ** 2))) if len(a[i:i + janela]) else 0.0
            niveis.append(float(20 * np.log10(rms / 32768 + 1e-9)))
    proc.wait()
    picos = _achar_picos(niveis, janela / taxa)
    cache.write_text(json.dumps(picos), encoding="utf-8")
    return picos


def _achar_picos(niveis: list[float], passo: float, max_picos: int = 80) -> list[dict]:
    """Janelas bem mais altas que o normal (mediana + 10 dB e acima dos 10% mais altos), juntando as vizinhas."""
    if len(niveis) < 20:
        return []
    ordenados = sorted(niveis)
    mediana = ordenados[len(ordenados) // 2]
    limiar = max(mediana + 10, ordenados[int(len(ordenados) * 0.9)] + 3)
    picos: list[dict] = []
    for i, db in enumerate(niveis):
        if db < limiar:
            continue
        ini = i * passo
        if picos and ini - picos[-1]["fim"] <= 1.5:
            picos[-1]["fim"] = ini + passo
            picos[-1]["db"] = max(picos[-1]["db"], round(db - mediana, 1))
        else:
            picos.append({"inicio": ini, "fim": ini + passo, "db": round(db - mediana, 1)})
    picos = sorted(picos, key=lambda p: -p["db"])[:max_picos]  # só os mais fortes
    return sorted(picos, key=lambda p: p["inicio"])


def _transcricao_com_picos(segmentos: list[dict], picos: list[dict]) -> str:
    """Transcrição em linhas, marcando com 🔊 as falas com pico de volume (e os picos sem fala)."""
    linhas = []
    usados = set()
    for s in segmentos:
        aqui = [i for i, p in enumerate(picos) if p["inicio"] < s["fim"] and p["fim"] > s["inicio"]]
        usados.update(aqui)
        marca = f" [🔊 volume alto +{max(picos[i]['db'] for i in aqui):.0f} dB]" if aqui else ""
        linhas.append((s["inicio"], f"[{s['inicio']:.1f}s | {_fmt(s['inicio'])}] {s['texto']}{marca}"))
    for i, p in enumerate(picos):
        if i not in usados:
            linhas.append((p["inicio"], f"[{p['inicio']:.1f}s | {_fmt(p['inicio'])}] "
                                        f"[🔊 volume alto +{p['db']:.0f} dB, sem fala]"))
    return "\n".join(t for _, t in sorted(linhas))


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
                    "pico": {"type": "number", "description": "segundo do ponto alto do corte: a reação (grito, "
                             "risada, 'QUÊ?!'), a punchline ou a cena chocante"},
                    "titulo": {"type": "string", "description": "título curto e chamativo, até 8 palavras"},
                    "descricao": {"type": "string", "description": "legenda do post: 1-2 frases que dão vontade "
                                  "de assistir + 3 a 5 hashtags (jogo, streamer, tema), sem #shorts"},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "5 a 10 termos de busca do YouTube (sem #)"},
                    "nota": {"type": "integer", "description": "potencial de viralizar de 1 a 10"},
                },
                "required": ["inicio", "fim", "pico", "titulo", "descricao", "nota", "tags"],
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


FORMATO_CORTES = ('{"cortes": [{"inicio": 12.3, "fim": 40.1, "pico": 14.0, "titulo": "...", '
                  '"descricao": "... #hashtag #hashtag #hashtag", "nota": 8, "tags": ["termo", "termo"]}]}')


def perguntar_ia(sistema: str, conteudo: str, op: Opcoes, log: Log, schema: dict, formato: str,
                 tarefa: str) -> dict:
    """Pergunta à IA e devolve JSON: pela API (se houver chave) ou pelo Claude Code (assinatura).
    Se falhar (limite de uso, internet, resposta quebrada), espera e tenta de novo até 4 vezes."""
    esperas = [30, 120, 300]
    for tentativa in range(len(esperas) + 1):
        try:
            if op.api_key.strip() or os.environ.get("ANTHROPIC_API_KEY"):
                return _ia_pela_api(sistema, conteudo, op, log, schema, tarefa)
            return _ia_pelo_claude_code(sistema, conteudo, log, formato, tarefa)
        except Exception as e:
            if re.search(r"authenticat|invalid x-api-key|not logged in|please run /login", str(e), re.I):
                # login vencido ou chave errada não se resolve esperando: avisa na hora como resolver
                raise RuntimeError(
                    f"{e}\n\nA IA não aceitou o login. Se usa a assinatura: abra o PowerShell, rode  claude  e "
                    "digite  /login . Se usa chave de API: confira a chave no console.anthropic.com.") from e
            if tentativa == len(esperas):
                raise
            log(f"  ⚠ A IA falhou ({str(e).splitlines()[0][:160]}). Tentando de novo em {esperas[tentativa]} s...")
            time.sleep(esperas[tentativa])


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
    try:
        saida = json.loads(r.stdout)
    except Exception:
        saida = {}
    if r.returncode != 0 or saida.get("is_error"):
        motivo = saida.get("result") or (r.stderr or r.stdout)[-400:]
        status = saida.get("api_error_status")
        raise RuntimeError(f"Claude Code: {motivo}" + (f" (erro {status})" if status else ""))
    resposta = saida.get("result", "")
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


def _caber_na_duracao(dentro: list[Palavra], inicios_frase: list[float], dur_max: float) -> tuple[list[Palavra], str]:
    """Se o corte passou da duração máxima, tira a preparação do começo (a partir de um início de frase),
    porque o final costuma ter a punchline. Se para isso tiver que jogar fora mais da metade, corta o fim."""
    maximo = dur_max - 0.6
    ini, fim = dentro[0].inicio, dentro[-1].fim
    if fim - ini <= maximo:
        return dentro, ""
    for t in inicios_frase:  # o 1º início de frase que faz caber = o menor corte possível
        if t <= ini:
            continue
        resto = [w for w in dentro if w.inicio >= t - 0.3]
        if resto and fim - resto[0].inicio <= maximo:
            if resto[0].inicio - ini <= (fim - ini) / 2:
                return resto, "começo"
            break
    return [w for w in dentro if w.fim <= ini + maximo] or dentro[:1], "fim"


MAX_ANTES_DO_PICO = 4.0  # o corte começa no máximo isso antes do ponto alto (o resto é preparação)


def _comecar_perto_do_pico(dentro: list[Palavra], inicios_frase: list[float], pico: float | None,
                           minimo: float = 6.0) -> list[Palavra]:
    """Tira a preparação: o corte passa a começar no início de frase mais próximo antes do pico, sem ficar
    mais de MAX_ANTES_DO_PICO antes dele. Não mexe se o pico for inválido ou se sobrar menos de `minimo` s."""
    if pico is None or not dentro or not dentro[0].inicio < pico < dentro[-1].fim:
        return dentro
    if pico - dentro[0].inicio <= MAX_ANTES_DO_PICO:
        return dentro
    limite = pico - MAX_ANTES_DO_PICO
    frases = [t for t in inicios_frase if limite <= t <= pico]
    # sem início de frase na janela: começa na primeira palavra dentro dela
    novo = frases[0] if frases else next((w.inicio for w in dentro if w.inicio >= limite), None)
    if novo is None:
        return dentro
    resto = [w for w in dentro if w.inicio >= novo - 0.3]
    if not resto or dentro[-1].fim - resto[0].inicio < minimo:
        return dentro
    return resto


def escolher_cortes(segmentos: list[dict], op: Opcoes, log: Log, picos: list[dict] | None = None,
                    inicio_min: float = 0.0) -> list[Corte]:
    if not segmentos:
        raise RuntimeError("A transcrição veio vazia — o vídeo tem fala?")

    transcricao = _transcricao_com_picos(segmentos, picos or [])

    sistema = (
        "Você é um editor especialista em cortes virais para TikTok, Reels e YouTube Shorts. "
        "Você recebe a transcrição de um vídeo longo, com o segundo de início de cada fala entre colchetes, "
        "e escolhe os trechos com maior potencial de prender a atenção: histórias completas, opiniões fortes, "
        "humor, revelações, conselhos práticos, momentos polêmicos ou emocionantes. "
        "Cada corte precisa fazer sentido sozinho para quem não viu o vídeo: começa num gancho forte "
        "(sem começar no meio de uma frase) e termina quando a ideia fecha. Cortes não podem se sobrepor.\n\n"
        "O que faz um Short performar (use como critério):\n"
        "- Os 2 primeiros segundos decidem tudo: quem não vê nada acontecendo em 1-2 s passa para o próximo. "
        "Comece NO pico ou no máximo 2-3 s antes dele: a reação (grito, risada, 'QUÊ?!'), a cena chocante ou a "
        "frase mais forte. Corte toda a preparação: menu, pausa, carregamento, 'deixa eu ver aqui', 'então', "
        "'bom', explicação. Informe em 'pico' o segundo desse ponto alto.\n"
        "- Um momento só por corte, com começo, clímax e fim; termine logo depois da punchline ou da reação, "
        "sem 'rabo' depois do ponto alto.\n"
        "- Reações fortes (riso, raiva, susto, indignação), frases citáveis e situações absurdas do jogo "
        "rendem mais que conversa morna ou leitura de chat.\n"
        "- Corte sem dó: curto e denso vence longo com partes paradas. Um corte de reação rende muito mais "
        "com 20 a 35 segundos, porque é visto até o fim e repetido (isso pesa muito no algoritmo). Só use mais "
        "tempo se a história realmente precisar. Tire falas que não levam à piada.\n"
        "- Título: até ~60 caracteres, curioso e fiel ao que acontece (sem mentir), em linguagem de internet; "
        "pode usar 1 emoji. Não comece com o nome do jogo.\n"
        "- 'nota' é sua aposta honesta de desempenho (1-10); reserve 9-10 para momentos realmente fortes.\n"
        "- Marcas [🔊 volume alto] indicam picos de som (grito, risada, susto, explosão no jogo). São fortes "
        "candidatos a reação, mas confira pelo contexto da fala: música alta ou propaganda também disparam."
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
    if inicio_min > 0:
        pedido += (f"\n\nO começo deste vídeo, até {inicio_min:.0f}s, repete o fim do bloco anterior da live e "
                   "já foi cortado: só escolha cortes que comecem depois disso (use esse trecho só como contexto).")
    if op.instrucoes_extras.strip():
        pedido += f"\n\nInstruções extras do usuário: {op.instrucoes_extras.strip()}"

    conteudo = f"<transcricao>\n{transcricao}\n</transcricao>\n\n{pedido}"
    dados = perguntar_ia(sistema, conteudo, op, log, SCHEMA_CORTES, FORMATO_CORTES, "escolher os melhores momentos")

    # todas as palavras com tempo, para alinhar os cortes certinho
    palavras = [Palavra(w["inicio"], w["fim"], w["texto"]) for s in segmentos for w in s["palavras"]]
    inicios_frase = sorted(s["inicio"] for s in segmentos)
    cortes: list[Corte] = []
    for c in dados["cortes"]:
        ini, fim = float(c["inicio"]), float(c["fim"])
        if fim <= ini:
            continue
        dentro = [w for w in palavras if w.inicio >= ini - 0.5 and w.fim <= fim + 1.5]
        if not dentro:
            continue
        pico = float(c["pico"]) if c.get("pico") is not None else None
        antes = dentro[0].inicio
        dentro = _comecar_perto_do_pico(dentro, inicios_frase, pico)
        if dentro[0].inicio - antes > 1:
            log(f"  (corte '{c['titulo'][:40]}': tirei {dentro[0].inicio - antes:.0f}s de preparação antes do pico)")
        dentro, onde = _caber_na_duracao(dentro, inicios_frase, op.dur_max)
        if onde:
            log(f"  (corte '{c['titulo'][:40]}' passou de {op.dur_max}s; encurtado no {onde})")
        ini = max(0.0, dentro[0].inicio - 0.15)
        fim = dentro[-1].fim + 0.35
        if ini < inicio_min - 1:  # trecho repetido do bloco anterior, que já foi cortado
            continue
        # evita sobreposição com cortes já aceitos
        if any(ini < o.fim and fim > o.inicio for o in cortes):
            continue
        cortes.append(Corte(ini, fim, c["titulo"].strip(), c["descricao"].strip(),
                            int(c["nota"]), dentro, tags=list(c.get("tags") or [])[:15], pico=pico))
    log(f"A IA escolheu {len(cortes)} cortes.")
    for i, c in enumerate(cortes, 1):
        log(f"  {i}. [{_fmt(c.inicio)} → {_fmt(c.fim)}] nota {c.nota} — {c.titulo}")
    return cortes


# ----------------------------------------------------------------------------
# 4. Edição
# ----------------------------------------------------------------------------
CAM_H, GAME_H = 768, 1152  # layout streamer: webcam em cima (1080x768), jogo embaixo (1080x1152)

PAUSA_MAX = 0.5  # silêncio maior que isso entre duas falas vira corte seco
FOLGA_PAUSA = 0.12  # quanto de respiro fica antes e depois de cada corte seco


def trechos_sem_pausa(palavras: list[Palavra], ini: float, fim: float,
                      picos: list[dict] | None = None) -> list[tuple[float, float]]:
    """Partes do corte que ficam, tirando os silêncios entre falas. Pausa com pico de volume (explosão,
    grito, susto no jogo) fica, porque ali está acontecendo alguma coisa."""
    trechos: list[tuple[float, float]] = []
    a = ini
    for w1, w2 in zip(palavras, palavras[1:]):
        de, ate = w1.fim + FOLGA_PAUSA, w2.inicio - FOLGA_PAUSA
        if w2.inicio - w1.fim <= PAUSA_MAX or ate <= de:
            continue
        if any(p["inicio"] < w2.inicio and p["fim"] > w1.fim for p in picos or []):
            continue
        if de - a >= 0.2:
            trechos.append((a, de))
            a = ate
    trechos.append((a, fim))
    return trechos


def _mapear_tempo(t: float, trechos: list[tuple[float, float]]) -> float:
    """Segundo no vídeo original -> segundo no corte já sem as pausas (dentro de uma pausa: vai pro fim dela)."""
    feito = 0.0
    for a, b in trechos:
        if t < a:
            return feito
        if t <= b:
            return feito + t - a
        feito += b - a
    return feito


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
        # só precisa da imagem: se não houver vídeo+áudio num arquivo só, serve um formato só de vídeo
        info = info_do_link(fonte, "best[height<=1080]/bv*[height<=1080]/best/bv*")
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
# vídeo vertical: distância da base da legenda até o pé do vídeo, em fração da altura
# (0.40 = a legenda fica um pouco abaixo do meio; aumente para subir, diminua para descer)
ALTURA_LEGENDA = 0.40


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
    clip = f"\\clip(0,{y0},{largura},{y0 + alt})"
    # listras diagonais sutis (textura) por toda a faixa
    listras = " ".join(f"m {x} 0 l {x + 26} 0 l {x + 26 - alt} {alt} l {x - alt} {alt}"
                       for x in range(0, largura + alt, 64))
    # brilho que atravessa a faixa logo que ela aparece
    brilho = f"m 0 0 l 90 0 l {90 - alt} {alt} l {-alt} {alt}"
    # cantos amarelos (triângulos) nas pontas de baixo
    canto = 34
    return [
        ret(y0 + alt, 10, "00006E", 2),            # sombra vinho embaixo
        ret(y0, alt, "1C1CD9", 3),                 # faixa vermelha
        ret(y0 + alt // 2, alt - alt // 2, "1212B4", 3, "\\1a&H70&"),  # metade de baixo mais escura (degradê)
        f"Dialogue: 3,{_ass_tempo(0)},{fim},Desenho,,0,0,0,,"
        f"{{\\an7\\pos(0,{y0})\\1c&H0E0EA0&\\1a&HB4&\\fad(150,0){clip}\\p1}}{listras}{{\\p0}}",
        ret(y0, 8, "00D2FF", 4),                   # detalhe amarelo em cima
        ret(y0 + 8, 3, "FFFFFF", 4, "\\1a&H60&"),  # filete branco embaixo do amarelo
        ret(y0 + alt - 4, 4, "0E0E9A", 4),         # filete escuro na base
        f"Dialogue: 4,{_ass_tempo(0)},{fim},Desenho,,0,0,0,,"
        f"{{\\an7\\pos(0,{y0 + alt - canto})\\1c&H00D2FF&\\fad(150,0)\\p1}}m 0 0 l {canto} {canto} l 0 {canto}{{\\p0}}",
        f"Dialogue: 4,{_ass_tempo(0)},{fim},Desenho,,0,0,0,,"
        f"{{\\an7\\pos({largura - canto},{y0 + alt - canto})\\1c&H00D2FF&\\fad(150,0)\\p1}}"
        f"m {canto} 0 l {canto} {canto} l 0 {canto}{{\\p0}}",
        f"Dialogue: 4,{_ass_tempo(0)},{_ass_tempo(min(dur, 1.2))},Desenho,,0,0,0,,"
        f"{{\\an7\\move({-120},{y0},{largura + alt},{y0},250,900)\\1c&HFFFFFF&\\1a&H90&{clip}\\p1}}{brilho}{{\\p0}}",
        f"Dialogue: 5,{_ass_tempo(0)},{fim},TitV,,0,0,0,,"
        f"{{\\an5\\pos({largura // 2},{y0 + alt // 2 + 3})\\fad(150,0)\\3c&H00006E&\\4c&H00005A&\\shad5"
        f"\\fscx80\\fscy80\\t(0,220,\\fscx100\\fscy100)}}{texto}",
    ]


def gerar_ass(corte: Corte, largura: int, altura: int, op: Opcoes, margem_tit: int | None = None,
              titulo_vermelho: bool = False, margem_leg: int | None = None,
              trechos: list[tuple[float, float]] | None = None) -> str:
    # tempos do vídeo original -> tempos no corte (sem as pausas, se elas foram tiradas)
    trechos = trechos or [(corte.inicio, corte.fim)]
    dur = sum(b - a for a, b in trechos)

    def tempo(t: float) -> float:
        return _mapear_tempo(t, trechos)
    tam_leg = int(altura * 0.045) if altura > largura else int(altura * 0.07)
    tam_tit = int(altura * 0.032) if altura > largura else int(altura * 0.055)
    if margem_leg is None:
        margem_leg = int(altura * ALTURA_LEGENDA) if altura > largura else int(altura * 0.10)
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
                    or tempo(w.inicio) - tempo(grupos[-1][-1].fim) > 0.6):
                grupos.append([])
            grupos[-1].append(w)
        for g in grupos:
            for i, w in enumerate(g):
                ini = tempo(w.inicio)
                fim = tempo(g[i + 1].inicio if i + 1 < len(g) else w.fim)
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


def _tem_audio(video: Path) -> bool:
    r = _run([achar_exe("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
              "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _entrada_do_corte(trechos: list[tuple[float, float]], base: float, audio: bool) -> str:
    """Filtro que recorta cada trecho no quadro exato e junta todos (corte seco) em [vin] e [ain]."""
    partes, rotulos = [], ""
    for i, (a, b) in enumerate(trechos):
        partes.append(f"[0:v]trim=start={a - base:.3f}:end={b - base:.3f},setpts=PTS-STARTPTS[v{i}]")
        rotulos += f"[v{i}]"
        if audio:
            partes.append(f"[0:a]atrim=start={a - base:.3f}:end={b - base:.3f},asetpts=PTS-STARTPTS[a{i}]")
            rotulos += f"[a{i}]"
    if len(trechos) == 1:
        return ";".join(partes).replace("[v0]", "[vin]").replace("[a0]", "[ain]")
    return ";".join(partes) + f";{rotulos}concat=n={len(trechos)}:v=1:a={int(audio)}[vin]" + ("[ain]" if audio else "")


def renderizar(video: Path, corte: Corte, destino: Path, op: Opcoes, log: Log) -> None:
    vw, vh = dimensoes_video(video)
    trechos = [(corte.inicio, corte.fim)]
    if op.cortar_pausas and len(corte.palavras) > 1:
        try:
            picos = picos_de_audio(video, log)
        except Exception:  # sem os picos, corta só pelas falas
            picos = []
        trechos = trechos_sem_pausa(corte.palavras, corte.inicio, corte.fim, picos)
    dur = sum(b - a for a, b in trechos)
    if len(trechos) > 1:
        log(f"    (tirei {corte.fim - corte.inicio - dur:.1f}s de pausas: {len(trechos) - 1} cortes secos)")
    zoom = f"(1+0.10*t/{dur:.2f})"  # aproximação lenta de 10% ao longo do corte

    def com_zoom(W, H):
        return [f"scale=w='2*trunc({W}*{zoom}/2)':h='2*trunc({H}*{zoom}/2)':eval=frame", f"crop={W}:{H}"]

    # Busca em 2 etapas: o -ss antes do -i pula rápido para alguns segundos antes do corte e o
    # trim no filtro corta no quadro exato. Só com o -ss antes do -i, em arquivos .ts de live o
    # vídeo começava no próximo quadro-chave (até 2 s depois) e o início ficava congelado.
    base = corte.inicio - min(corte.inicio, 3.0)
    audio = _tem_audio(video)
    entrada = _entrada_do_corte(trechos, base, audio)
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
            grafo = (f"[vin]split=3[a][b][c];"
                     f"[a]crop={cw}:{ch}:{cx}:{cy},scale=1080:{CAM_H},{cam_zoom}setsar=1[cam];"
                     f"[b]scale=1080:{GAME_H}:force_original_aspect_ratio=increase,crop=1080:{GAME_H},"
                     f"boxblur=25:2,eq=brightness=-0.12,setsar=1[fundo];"
                     f"[c]scale=1080:{jogo_h},setsar=1[jogo];"
                     f"[fundo][jogo]overlay=0:{gy}[baixo];"
                     f"[cam][baixo]vstack=inputs=2")
            margem_tit = CAM_H  # faixa do título encostada embaixo da webcam
            margem_leg = int(H * ALTURA_LEGENDA)  # legenda por cima do jogo, um pouco abaixo do meio

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
            ass = gerar_ass(corte, W, H, op, margem_tit, titulo_vermelho=grafo is not None, margem_leg=margem_leg,
                            trechos=trechos)
            Path(tmp, "leg.ass").write_text(ass, encoding="utf-8")
            filtros.append("subtitles=leg.ass")
        filtros.append("setsar=1")

        meio = f"{grafo}[st];[st]" if grafo else "[vin]"
        video_args = ["-filter_complex", f"{entrada};{meio}{','.join(filtros)}[v]", "-map", "[v]"]
        if audio:
            video_args += ["-map", "[ain]"]
        cmd = [
            achar_exe("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{base:.3f}", "-i", str(video.resolve()),
            *video_args, "-t", f"{dur:.3f}",
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


def refazer_corte(pasta: Path, arquivo: str, inicio: float, fim: float, titulo: str, op: Opcoes, log: Log) -> None:
    """Renderiza de novo um corte com outro início/fim/título (tela de revisão). Usa a transcrição salva."""
    r = next(x for x in json.loads((pasta / "cortes.json").read_text(encoding="utf-8")) if x["arquivo"] == arquivo)
    video = Path(r.get("video", ""))
    if not r.get("video") or not video.exists():
        raise RuntimeError("O vídeo original não está mais no PC (blocos de live são apagados depois de cortados), "
                           "então não dá para mudar início e fim deste corte.")
    if fim - inicio < 3:
        raise RuntimeError("O corte precisa ter pelo menos 3 segundos.")
    segmentos = transcrever(video, op, log)
    palavras = [Palavra(w["inicio"], w["fim"], w["texto"]) for s in segmentos for w in s["palavras"]
                if w["inicio"] >= inicio - 0.05 and w["fim"] <= fim + 0.05]
    corte = Corte(inicio, fim, titulo.strip() or r["titulo"], r.get("descricao", ""), r.get("nota", 0), palavras)
    log(f"Refazendo '{arquivo}' ({_fmt(inicio)} → {_fmt(fim)})...")
    # renderiza num arquivo à parte: se o vídeo estiver aberto no player, o original continua inteiro
    tmp = pasta / f"refazendo-{arquivo}"
    renderizar(video, corte, tmp, op, log)
    try:
        os.replace(tmp, pasta / arquivo)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Feche o vídeo no player e tente de novo (o Windows não deixa trocar um arquivo aberto).")

    def mudar(lista):
        for x in lista:
            if x["arquivo"] == arquivo:
                x.update(inicio=round(inicio, 2), fim=round(fim, 2), duracao=round(fim - inicio, 1),
                         titulo=corte.titulo, fala=" ".join(w.texto for w in palavras)[:2000])
    alterar_cortes_json(pasta, mudar)
    log("  pronto.")


def cortar_video(video: Path, pasta: Path, op: Opcoes, log: Log, prefixo: str = "", fonte: str = "",
                 inicio_min: float = 0.0) -> list[Corte]:
    """Transcreve, escolhe e edita os cortes de um arquivo. Retorna os cortes que saíram."""
    segmentos = transcrever(video, op, log)
    if not segmentos:
        log("  (sem fala neste trecho, pulando)")
        return []
    try:
        picos = picos_de_audio(video, log)
    except Exception as e:  # sem os picos a IA ainda escolhe pela fala
        log(f"  (não consegui medir o volume: {e})")
        picos = []
    cortes = escolher_cortes(segmentos, op, log, picos, inicio_min)
    pasta.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(cortes, 1):
        nome = f"{prefixo}{i:02d} - {_nome_arquivo(c.titulo)}.mp4"
        log(f"Editando corte {i}/{len(cortes)}: {c.titulo}")
        renderizar(video, c, pasta / nome, op, log)
        r = {"arquivo": nome, "titulo": c.titulo, "descricao": c.descricao, "tags": c.tags, "fonte": fonte,
             "fala": " ".join(w.texto for w in c.palavras)[:2000], "duracao": round(c.fim - c.inicio, 1),
             "nota": c.nota, "origem": video.name, "video": str(video.resolve()),
             "inicio": round(c.inicio, 2), "fim": round(c.fim, 2),
             "pico": round(c.pico, 2) if c.pico is not None else None}
        if op.publicar_youtube and op.revisar_antes:
            pass  # vai para a tela de revisão no final; nada sobe sem você aprovar
        elif op.publicar_youtube:
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
                    if publicador.eh_limite(e):
                        quando = publicador.por_na_fila(pasta, op.yt_modo, op.yt_intervalo_min, op.yt_nota_min)
                        log(f"  Limite diário do YouTube: os cortes desta pasta vão para a fila "
                            f"(nova tentativa a partir de {quando:%d/%m %H:%M}).")
            else:
                log(f"  (nota {c.nota} abaixo da mínima {op.yt_nota_min}: não publicado)")
        alterar_cortes_json(pasta, lambda dados: dados.append(r))
        with open(pasta / "legendas_dos_posts.txt", "a", encoding="utf-8") as f:
            f.write(f"{r['arquivo']}\n{r['titulo']}\n{r['descricao']}\n\n")
    return cortes


# ----------------------------------------------------------------------------
# Completar legenda, hashtags e tags de cortes antigos
# ----------------------------------------------------------------------------
SCHEMA_LEGENDAS = {
    "type": "object",
    "properties": {
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "arquivo": {"type": "string"},
                    "descricao": {"type": "string", "description": "1-2 frases + 3 a 5 hashtags, sem #shorts"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["arquivo", "descricao", "tags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["itens"],
    "additionalProperties": False,
}
FORMATO_LEGENDAS = '{"itens": [{"arquivo": "...", "descricao": "... #hashtag #hashtag #hashtag", "tags": ["termo"]}]}'


def precisa_legenda(r: dict) -> bool:
    return not r.get("tags") or "#" not in r.get("descricao", "")


def completar_legendas(pasta: Path, op: Opcoes, log: Log, fonte: str = "", incluir_publicados: bool = False,
                       refazer: bool = False) -> int:
    """Pede à IA legenda + hashtags + tags para os cortes da pasta que estão sem (em lotes de 15)."""
    try:
        import aprendizado
        import publicador
        publicados = publicador.publicados()
    except Exception:
        aprendizado, publicados = None, {}
    dados = json.loads((pasta / "cortes.json").read_text(encoding="utf-8"))
    alvos = [r for r in dados if (refazer or precisa_legenda(r))
             and (incluir_publicados or f"{pasta.name}/{r['arquivo']}" not in publicados)]
    if fonte:
        alterar_cortes_json(pasta, lambda lista: [x.setdefault("fonte", fonte) for x in lista if not x.get("fonte")])
    if not alvos:
        return 0
    log(f"{len(alvos)} cortes sem hashtags/tags em '{pasta.name}': pedindo para a IA completar...")
    sistema = (
        "Você escreve a legenda de YouTube Shorts de um canal de cortes de lives de jogos, em português do Brasil. "
        "Para cada corte: 'descricao' com 1 ou 2 frases que deem vontade de assistir (fiéis ao que acontece), "
        "seguidas de 3 a 5 hashtags relevantes (o jogo, o streamer, o tema do momento; sem #shorts); "
        "'tags' com 5 a 10 termos de busca do YouTube, sem #. Devolva um item para cada 'arquivo' recebido, "
        "copiando o nome do arquivo exatamente. Se o link da live mostrar quem é o streamer, inclua o nome dele "
        "como hashtag (ex.: #alanzoka para twitch.tv/alanzoka) e como tag."
    )
    feitos = 0
    for i in range(0, len(alvos), 15):
        lote = alvos[i:i + 15]
        itens = []
        for r in lote:
            fala = r.get("fala") or (aprendizado._fala_do_cache(pasta, r) if aprendizado else "")
            itens.append({"arquivo": r["arquivo"], "titulo": r.get("titulo", ""), "live": r.get("fonte") or fonte,
                          "descricao_atual": r.get("descricao", ""), "fala": fala[:600]})
        conteudo = "Cortes:\n" + json.dumps(itens, ensure_ascii=False, indent=1)
        resp = perguntar_ia(sistema, conteudo, op, log, SCHEMA_LEGENDAS, FORMATO_LEGENDAS,
                            f"escrever legendas e hashtags ({len(lote)} cortes)")
        novos = {x["arquivo"]: x for x in resp.get("itens", []) if x.get("arquivo")}

        def aplicar(lista):
            for x in lista:
                n = novos.get(x["arquivo"])
                if n and n.get("descricao", "").strip():
                    x["descricao"] = n["descricao"].strip()
                    x["tags"] = [t.strip() for t in n.get("tags", []) if t.strip()][:15]

        alterar_cortes_json(pasta, aplicar)
        feitos += sum(1 for r in lote if r["arquivo"] in novos)
    # arquivo de legendas para copiar e colar, refeito com os textos novos
    final = json.loads((pasta / "cortes.json").read_text(encoding="utf-8"))
    with open(pasta / "legendas_dos_posts.txt", "w", encoding="utf-8") as f:
        for r in final:
            f.write(f"{r['arquivo']}\n{r['titulo']}\n{r['descricao']}\n\n")
    log(f"Legendas completadas: {feitos} cortes.")
    return feitos


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


def _pasta_livre(pasta: Path) -> Path:
    """Rodar o mesmo vídeo de novo vai para 'nome (2)', 'nome (3)'...: os cortes da vez anterior
    (e o registro de quais foram para o YouTube) ficam intactos, sem misturar arquivos velhos e novos."""
    base, n = pasta, 2
    while pasta.exists() and any(pasta.iterdir()):
        pasta = base.with_name(f"{base.name} ({n})")
        n += 1
    return pasta


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
    pasta = _pasta_livre(PASTA_SAIDA / _nome_arquivo(video.stem))
    n = len(cortar_video(video, pasta, op, log, fonte=fonte.strip() if eh_link(fonte) else ""))
    if not n:
        raise RuntimeError("A IA não encontrou cortes bons neste vídeo.")
    log(f"Pronto! {n} cortes salvos em: {pasta}")
    return pasta


# ----------------------------------------------------------------------------
# Live ao vivo: grava em blocos e corta cada bloco assim que ele fecha
# ----------------------------------------------------------------------------
def info_do_link(url: str, formato: str = "bv*+ba/b") -> dict:
    # "bv*+ba/b" (e não "best"): funciona mesmo quando o YouTube não oferece vídeo+áudio num arquivo só
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                           "format": formato, **_runtime_js()}) as ydl:
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
        # algumas lives só têm vídeo e áudio separados: aí o FFmpeg recebe os dois e junta na gravação
        info = info_do_link(url_live, "best[height<=1080][protocol^=m3u8]"
                                      "/bv*[height<=1080][protocol^=m3u8]+ba[protocol^=m3u8]"
                                      "/best[height<=1080]/bv*[height<=1080]+ba/best/bv*+ba")
        partes = info.get("requested_formats") or [info]
        entrada = []
        for f in partes:
            cabecalhos = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
            entrada += (["-headers", cabecalhos] if cabecalhos else []) + ["-i", f["url"]]
        if len(partes) > 1:
            entrada += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd = [achar_exe("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *entrada,
           "-c", "copy", "-f", "segment", "-segment_time", str(op.bloco_min * 60),
           "-reset_timestamps", "1", str(pasta_blocos / f"r{rodada:02d}_bloco_%03d.ts")]
    log(f"Gravando a live em blocos de {op.bloco_min} min...")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


SOBRA_BLOCO_S = 120  # quanto do fim de um bloco da live entra junto no começo do próximo


def _juntar_ts(partes: list[Path], destino: Path) -> None:
    lista = destino.with_suffix(".txt")
    lista.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in partes), encoding="utf-8")
    try:
        _run([achar_exe("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lista),
              "-c", "copy", str(destino)], capture_output=True, check=True)
    finally:
        lista.unlink(missing_ok=True)


class _EmendaDosBlocos:
    """Corta cada bloco junto com o fim do anterior, para um momento bem na divisa não se perder.
    Só aceita cortes que começam depois do último corte do bloco anterior (sem repetir)."""

    def __init__(self, pasta_blocos: Path):
        self.rabo: Path | None = None   # fim do bloco anterior
        self.livre_desde = 0.0          # segundo, dentro do rabo, onde acabou o último corte já feito
        self.pasta = pasta_blocos

    def cortar(self, bloco: Path, n: int, pasta: Path, op: Opcoes, log: Log, url: str) -> int:
        entrada, inicio_min = bloco, 0.0
        if self.rabo and self.rabo.exists():
            entrada = self.pasta / f"junto-{n:03d}.ts"
            _juntar_ts([self.rabo, bloco], entrada)
            inicio_min = self.livre_desde
        dur_entrada = _duracao(entrada)
        try:
            cortes = cortar_video(entrada, pasta, op, log, prefixo=f"b{n:02d}-", fonte=url, inicio_min=inicio_min)
        except Exception:
            self.descartar()  # o bloco vai ser refeito sozinho no final; o próximo começa sem emenda
            raise
        finally:
            if entrada != bloco:
                entrada.unlink(missing_ok=True)
        novo = self.pasta / f"fim-anterior-{n:03d}.ts"
        _run([achar_exe("ffmpeg"), "-v", "error", "-y", "-sseof", f"-{SOBRA_BLOCO_S}", "-i", str(bloco),
              "-c", "copy", str(novo)], capture_output=True)
        self.descartar()
        if novo.exists():
            # o rabo novo começa em (duração cortada - duração do rabo) dentro do arquivo que foi cortado
            ultimo_fim = max((c.fim for c in cortes), default=0.0)
            self.rabo = novo
            self.livre_desde = max(0.0, ultimo_fim - (dur_entrada - _duracao(novo)))
        return len(cortes)

    def descartar(self) -> None:
        if self.rabo:
            self.rabo.unlink(missing_ok=True)
        self.rabo, self.livre_desde = None, 0.0


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
    falhos: list[tuple[int, Path]] = []
    n_bloco = 0
    emenda = _EmendaDosBlocos(pasta_blocos)
    vigia = {"tamanho": -1, "desde": time.time()}  # detecta gravação travada (sem dados chegando)

    # os blocos são cortados em outra thread: transcrever demora, e enquanto isso o vigia da
    # gravação (travou? caiu? pediram para parar?) precisa continuar rodando
    fila_blocos: queue.Queue = queue.Queue()
    contagem = {"total": 0}

    def cortador():
        while (item := fila_blocos.get()) is not None:
            n, bloco = item
            log(f"— Cortando o bloco {n}...")
            try:
                contagem["total"] += emenda.cortar(bloco, n, pasta, op, log, url)
                bloco.unlink(missing_ok=True)  # bloco já cortado: libera espaço (~3 GB por hora de live)
            except Exception as e:
                log(f"  Erro no bloco {n}: {e}")
                log("  O bloco foi guardado e vai ser cortado de novo no final da live.")
                falhos.append((n, bloco))
            log(f"— Bloco {n} pronto. Total até agora: {contagem['total']} cortes em {pasta}")

    trabalhador = threading.Thread(target=cortador, daemon=True)
    trabalhador.start()

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
            espera = fila_blocos.qsize()
            log(f"— Bloco {n_bloco} fechado ({dur / 60:.0f} min)."
                + (f" Na fila: o corte está mais lento que a live ({espera} blocos esperando)." if espera else ""))
            fila_blocos.put((n_bloco, bloco))

        if gravando and entrada_teste is None:
            atuais = sorted(pasta_blocos.glob(f"r{rodada:02d}_bloco_*.ts"))
            tamanho = sum(a.stat().st_size for a in atuais)
            if tamanho != vigia["tamanho"]:
                vigia.update(tamanho=tamanho, desde=time.time())
            elif time.time() - vigia["desde"] > 60:
                log("  ⚠ A gravação travou (nenhum dado da live há 60 s). Reconectando...")
                gravador.kill()
                gravador.wait()
                gravando = False

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
            vigia.update(tamanho=-1, desde=time.time())
            continue

        parar.wait(5)

    fila_blocos.put(None)
    if fila_blocos.qsize() > 1:
        log(f"Gravação encerrada. Terminando de cortar {fila_blocos.qsize() - 1} blocos que faltam...")
    trabalhador.join()
    total = contagem["total"]
    emenda.descartar()
    for n, bloco in falhos:  # segunda chance para os blocos que deram erro
        log(f"— Tentando de novo o bloco {n}...")
        try:
            total += len(cortar_video(bloco, pasta, op, log, prefixo=f"b{n:02d}-", fonte=url))
            bloco.unlink(missing_ok=True)
        except Exception as e:
            log(f"  O bloco {n} falhou de novo ({e}). O vídeo dele ficou guardado em: {bloco}")
    log(f"Pronto! {total} cortes da live salvos em: {pasta}")
    return pasta
