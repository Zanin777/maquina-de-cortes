"""Publicação automática no YouTube Shorts (API oficial do YouTube).

Login: o usuário entra na conta dele pelo navegador, numa tela do Google, uma única vez.
O programa guarda só a autorização (youtube_token.json), nunca a senha.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import motor

Log = Callable[[str], None]

ESCOPOS = ["https://www.googleapis.com/auth/youtube.upload",
           "https://www.googleapis.com/auth/youtube.readonly"]
ARQ_CLIENTE = motor.PASTA_APP / "youtube_cliente.json"   # credencial do app (baixada do Google Cloud)
ARQ_TOKEN = motor.PASTA_APP / "youtube_token.json"       # autorização da conta (criada no login)
ARQ_AGENDA = motor.PASTA_APP / "youtube_agenda.json"     # último horário agendado
CATEGORIA_JOGOS = "20"


def _credenciais(permitir_login: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    cred = None
    if ARQ_TOKEN.exists():
        cred = Credentials.from_authorized_user_file(str(ARQ_TOKEN), ESCOPOS)
    if cred and cred.valid:
        return cred
    if cred and cred.expired and cred.refresh_token:
        try:
            cred.refresh(Request())
            ARQ_TOKEN.write_text(cred.to_json(), encoding="utf-8")
            return cred
        except Exception:
            cred = None
    if not permitir_login:
        raise RuntimeError("Conta do YouTube não conectada. Clique em 'Conectar YouTube...' na janela.")
    if not ARQ_CLIENTE.exists():
        raise RuntimeError("Falta o arquivo de credencial do Google (veja o passo a passo no README).")
    from google_auth_oauthlib.flow import InstalledAppFlow

    fluxo = InstalledAppFlow.from_client_secrets_file(str(ARQ_CLIENTE), ESCOPOS)
    cred = fluxo.run_local_server(port=0, prompt="consent",
                                  authorization_prompt_message="",
                                  success_message="Pronto! Pode fechar esta aba e voltar para a Máquina de Cortes.")
    ARQ_TOKEN.write_text(cred.to_json(), encoding="utf-8")
    return cred


def _servico(permitir_login: bool = False):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_credenciais(permitir_login), cache_discovery=False)


def conectar(arquivo_cliente: str | None, log: Log) -> str:
    """Guarda a credencial do app (se vier uma nova), abre o login no navegador e devolve o nome do canal."""
    if arquivo_cliente:
        dados = json.loads(Path(arquivo_cliente).read_text(encoding="utf-8"))
        if "installed" not in dados:
            raise RuntimeError("Esse arquivo não é uma credencial do tipo 'App para computador'.\n"
                               "No Google Cloud, crie o ID do cliente OAuth com o tipo 'App para computador'.")
        shutil.copy(arquivo_cliente, ARQ_CLIENTE)
        ARQ_TOKEN.unlink(missing_ok=True)
    log("Abrindo o navegador para você entrar na sua conta do YouTube...")
    return nome_do_canal(_servico(permitir_login=True))


def nome_do_canal(servico=None) -> str:
    servico = servico or _servico()
    itens = servico.channels().list(part="snippet", mine=True).execute().get("items", [])
    return itens[0]["snippet"]["title"] if itens else "(conta sem canal)"


def conectado() -> bool:
    return ARQ_TOKEN.exists()


def desconectar() -> None:
    ARQ_TOKEN.unlink(missing_ok=True)


def proximo_horario(intervalo_h: float, agora: datetime | None = None) -> datetime:
    """Próximo horário livre na fila de agendamento (um Short a cada `intervalo_h` horas)."""
    agora = agora or datetime.now(timezone.utc)
    ultimo = None
    if ARQ_AGENDA.exists():
        try:
            ultimo = datetime.fromisoformat(json.loads(ARQ_AGENDA.read_text(encoding="utf-8"))["ultimo"])
        except Exception:
            ultimo = None
    candidato = agora + timedelta(minutes=15)
    if ultimo and ultimo + timedelta(hours=intervalo_h) > candidato:
        candidato = ultimo + timedelta(hours=intervalo_h)
    return candidato.replace(microsecond=0)


def montar_corpo(titulo: str, descricao: str, modo: str, publicar_em: datetime | None) -> dict:
    titulo = titulo.strip()
    if "#shorts" not in titulo.lower() and len(titulo) <= 90:
        titulo += " #shorts"
    titulo = titulo.replace("<", "").replace(">", "")[:100]
    descricao = (descricao.strip() + "\n\n#shorts").replace("<", "").replace(">", "")[:4900]
    status = {"selfDeclaredMadeForKids": False}
    if modo == "agendado" and publicar_em:
        status["privacyStatus"] = "private"  # o YouTube exige 'private' para agendar
        status["publishAt"] = publicar_em.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    elif modo == "nao_listado":
        status["privacyStatus"] = "unlisted"
    elif modo == "privado":
        status["privacyStatus"] = "private"
    else:
        status["privacyStatus"] = "public"
    return {"snippet": {"title": titulo, "description": descricao, "categoryId": CATEGORIA_JOGOS},
            "status": status}


def publicar(video: Path, titulo: str, descricao: str, modo: str, intervalo_h: float, log: Log) -> str:
    """Envia o vídeo e devolve o link do Short."""
    from googleapiclient.http import MediaFileUpload

    publicar_em = proximo_horario(intervalo_h) if modo == "agendado" else None
    corpo = montar_corpo(titulo, descricao, modo, publicar_em)
    servico = _servico()
    envio = servico.videos().insert(
        part="snippet,status", body=corpo, notifySubscribers=True,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True),
    )
    resposta = None
    while resposta is None:
        estado, resposta = envio.next_chunk()
        if estado:
            log(f"    enviando ao YouTube... {int(estado.progress() * 100)}%")
    if publicar_em:
        ARQ_AGENDA.write_text(json.dumps({"ultimo": publicar_em.isoformat()}), encoding="utf-8")
        local = publicar_em.astimezone().strftime("%d/%m %H:%M")
        log(f"    agendado para {local}")
    return f"https://youtube.com/shorts/{resposta['id']}"
