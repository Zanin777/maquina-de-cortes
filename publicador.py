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
ARQ_PUBLICADOS = motor.PASTA_APP / "youtube_publicados.json"  # tudo que já foi pro YouTube (fonte da verdade)
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


def proximo_horario(intervalo_min: float, agora: datetime | None = None) -> datetime:
    """Próximo horário livre na fila de agendamento (um Short a cada `intervalo_min` minutos)."""
    agora = agora or datetime.now(timezone.utc)
    ultimo = None
    if ARQ_AGENDA.exists():
        try:
            ultimo = datetime.fromisoformat(json.loads(ARQ_AGENDA.read_text(encoding="utf-8"))["ultimo"])
        except Exception:
            ultimo = None
    candidato = agora + timedelta(minutes=15)
    if ultimo and ultimo + timedelta(minutes=intervalo_min) > candidato:
        candidato = ultimo + timedelta(minutes=intervalo_min)
    return candidato.replace(microsecond=0)


def montar_corpo(titulo: str, descricao: str, modo: str, publicar_em: datetime | None,
                 tags: list[str] | None = None, fonte: str = "") -> dict:
    titulo = titulo.strip()
    if "#shorts" not in titulo.lower() and len(titulo) <= 90:
        titulo += " #shorts"
    titulo = titulo.replace("<", "").replace(">", "")[:100]
    if fonte:
        descricao = descricao.strip() + f"\n\n🎥 Live original: {fonte}"
    descricao = (descricao.strip() + "\n\n#shorts").replace("<", "").replace(">", "")[:4900]
    # tags: até ~450 caracteres no total (limite do YouTube é 500)
    if not tags:  # cortes antigos sem tags: usa as hashtags da legenda
        import re
        tags = [h.replace('_', ' ') for h in re.findall(r"#(\w+)", descricao) if h.lower() != 'shorts']
    lista, total = [], 0
    for t in (tags or []):
        t = t.replace("#", "").replace("<", "").replace(">", "").strip()
        if t and total + len(t) + 2 <= 450:
            lista.append(t)
            total += len(t) + 2
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
    return {"snippet": {"title": titulo, "description": descricao, "categoryId": CATEGORIA_JOGOS, "tags": lista},
            "status": status}


def publicar(video: Path, titulo: str, descricao: str, modo: str, intervalo_min: float, log: Log,
             tags: list[str] | None = None, fonte: str = "") -> str:
    """Envia o vídeo e devolve o link do Short."""
    from googleapiclient.http import MediaFileUpload

    publicar_em = proximo_horario(intervalo_min) if modo == "agendado" else None
    corpo = montar_corpo(titulo, descricao, modo, publicar_em, tags, fonte)
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


def _chave(video: Path) -> str:
    return f"{video.parent.name}/{video.name}"


def publicados() -> dict:
    """{'pasta/arquivo.mp4': {'titulo', 'link', 'data'}} — não depende do cortes.json."""
    if ARQ_PUBLICADOS.exists():
        try:
            return json.loads(ARQ_PUBLICADOS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def registrar(video: Path, titulo: str, link: str) -> None:
    dados = publicados()
    dados[_chave(video)] = {"titulo": titulo, "link": link, "data": datetime.now().isoformat(timespec="seconds")}
    tmp = ARQ_PUBLICADOS.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ARQ_PUBLICADOS)


def ja_publicado(video: Path, titulo: str = "") -> str | None:
    """Link do Short se esse corte já foi publicado (pelo arquivo ou, por segurança, pelo mesmo título)."""
    dados = publicados()
    if _chave(video) in dados:
        return dados[_chave(video)]["link"]
    for v in dados.values():
        if titulo and v.get("titulo") == titulo:
            return v["link"]
    return None


def recuperar_do_canal(log: Log) -> int:
    """Lê os vídeos do canal e registra os cortes que já estão lá (casando pelo título)."""
    servico = _servico()
    canal = servico.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = canal["contentDetails"]["relatedPlaylists"]["uploads"]
    no_canal, pagina = {}, None
    while True:
        resp = servico.playlistItems().list(part="snippet", playlistId=uploads, maxResults=50,
                                            pageToken=pagina).execute()
        for it in resp.get("items", []):
            t = it["snippet"]["title"].replace(" #shorts", "").strip()
            no_canal[t] = f"https://youtube.com/shorts/{it['snippet']['resourceId']['videoId']}"
        pagina = resp.get("nextPageToken")
        if not pagina:
            break
    achados = 0
    for arq in motor.PASTA_SAIDA.glob("*/cortes.json"):
        for r in json.loads(arq.read_text(encoding="utf-8")):
            titulo = r.get("titulo", "").replace("<", "").replace(">", "").strip()
            link = no_canal.get(titulo) or no_canal.get(titulo[:91].strip())
            if link and not ja_publicado(arq.parent / r["arquivo"]):
                registrar(arq.parent / r["arquivo"], r["titulo"], link)
                achados += 1
    log(f"{len(no_canal)} vídeos no canal; {achados} cortes reconhecidos e registrados como já publicados.")
    return achados


def pastas_de_cortes(pasta: Path) -> list[Path]:
    """A pasta escolhida, ou as subpastas dela que têm cortes (quando escolhem a pasta 'cortes' inteira)."""
    if (pasta / "cortes.json").exists():
        return [pasta]
    return sorted(p.parent for p in pasta.glob("*/cortes.json"))


def _fonte_da_pasta(pasta: Path) -> str:
    """Crédito da live para cortes antigos sem 'fonte': usa o de outro corte da mesma pasta."""
    try:
        for x in json.loads((pasta / "cortes.json").read_text(encoding="utf-8")):
            if x.get("fonte"):
                return x["fonte"]
    except Exception:
        pass
    return ""


def publicar_pasta(pasta: Path, modo: str, intervalo_min: float, nota_min: int, log: Log) -> int:
    """Publica os cortes de uma pasta que ainda não foram pro YouTube (melhores notas primeiro)."""
    pendentes = []
    for p in pastas_de_cortes(pasta):
        dados = json.loads((p / "cortes.json").read_text(encoding="utf-8"))
        for i, r in enumerate(dados):
            if r.get("youtube") or ja_publicado(p / r["arquivo"], r.get("titulo", "")) \
                    or not (p / r["arquivo"]).exists():
                continue
            if r.get("nota", 0) < nota_min:
                continue
            pendentes.append((r.get("nota", 0), p, r["arquivo"]))
    if not pendentes:
        log("Nenhum corte novo para publicar nessa pasta (ou todos abaixo da nota mínima).")
        return 0
    pendentes.sort(key=lambda x: -x[0])
    log(f"{len(pendentes)} cortes para publicar ({modo}" + (f", 1 a cada {intervalo_min:g} min)" if modo == "agendado" else ")"))
    feitos = 0
    for n, (nota, p, arquivo) in enumerate(pendentes, 1):
        dados = json.loads((p / "cortes.json").read_text(encoding="utf-8"))
        r = next((x for x in dados if x["arquivo"] == arquivo), None)
        if r is None or ja_publicado(p / arquivo, r.get("titulo", "")):
            continue
        log(f"[{n}/{len(pendentes)}] nota {nota} — {r['titulo']}")
        try:
            link = publicar(p / r["arquivo"], r["titulo"], r.get("descricao", ""), modo, intervalo_min, log,
                            tags=r.get("tags"), fonte=r.get("fonte", "") or _fonte_da_pasta(p))
        except Exception as e:
            log(f"  ⚠ Erro: {e}")
            if "quota" in str(e).lower() or "uploadLimitExceeded" in str(e):
                log("  Limite diário do YouTube atingido — o resto fica para amanhã (é só clicar de novo).")
                break
            continue
        registrar(p / arquivo, r["titulo"], link)

        def marcar(lista, arquivo=arquivo, link=link):
            for x in lista:
                if x["arquivo"] == arquivo:
                    x["youtube"] = link
        motor.alterar_cortes_json(p, marcar)
        log(f"  ✅ {link}")
        feitos += 1
    log(f"Pronto: {feitos} cortes enviados ao YouTube.")
    return feitos
