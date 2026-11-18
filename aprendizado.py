"""Aprendizado com o próprio canal.

1. Busca no YouTube o desempenho de cada Short publicado (visualizações, curtidas, comentários).
2. Compara os que foram melhor com os que foram pior e pede à IA regras para este canal.
3. Essas regras + exemplos reais entram nas instruções da IA na hora de escolher os próximos cortes.

Não muda o modelo da IA (isso não é possível); muda o que ela sabe sobre o seu público.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import motor

Log = Callable[[str], None]

ARQ_DESEMPENHO = motor.PASTA_APP / "desempenho.json"
ARQ_LICOES = motor.PASTA_APP / "licoes_do_canal.json"
IDADE_MIN_H = 24        # só compara Shorts com pelo menos 1 dia no ar
MIN_SHORTS = 6          # mínimo de Shorts maduros para tirar conclusões

SCHEMA_LICOES = {
    "type": "object",
    "properties": {
        "regras": {"type": "array", "items": {"type": "string"},
                   "description": "5 a 10 regras práticas e específicas para escolher e titular cortes neste canal"},
        "resumo": {"type": "string", "description": "2-3 frases sobre o que o público deste canal prefere"},
    },
    "required": ["regras", "resumo"],
    "additionalProperties": False,
}
FORMATO_LICOES = '{"regras": ["...", "..."], "resumo": "..."}'


# ----------------------------------------------------------------------------
# 1. Shorts publicados e o que foi falado em cada um
# ----------------------------------------------------------------------------
def _id_do_link(link: str) -> str:
    return link.rstrip("/").split("/")[-1].split("?")[0]


def _fala_do_cache(pasta_cortes: Path, r: dict) -> str:
    """Recupera a fala de cortes antigos (feitos antes de a fala ser salva) pela transcrição em cache."""
    m = re.search(r"ao vivo (\d\d-\d\d) (\d\dh\d\d)", pasta_cortes.name)
    candidatos = []
    if m and r.get("origem"):
        pasta_live = motor.PASTA_DOWNLOADS / f"live-{m.group(1)}_{m.group(2)}"
        candidatos = list(pasta_live.glob(Path(r["origem"]).stem + ".transcricao-*.json"))
    for c in candidatos:
        try:
            segs = json.loads(c.read_text(encoding="utf-8"))
        except Exception:
            continue
        palavras = [w["texto"] for s in segs for w in s.get("palavras", [])
                    if r["inicio"] - 0.5 <= w["inicio"] <= r["fim"] + 0.5]
        if palavras:
            return " ".join(palavras)[:2000]
    return ""


def publicados() -> list[dict]:
    import publicador

    registro = publicador.publicados()
    lista = []
    for arq in motor.PASTA_SAIDA.glob("*/cortes.json"):
        try:
            dados = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in dados:
            link = r.get("youtube") or registro.get(f"{arq.parent.name}/{r['arquivo']}", {}).get("link")
            if not link:
                continue
            lista.append({
                "id": _id_do_link(link),
                "titulo": r.get("titulo", ""),
                "fala": r.get("fala") or _fala_do_cache(arq.parent, r),
                "duracao": r.get("duracao") or round(r.get("fim", 0) - r.get("inicio", 0), 1),
                "nota_ia": r.get("nota"),
            })
    return lista


# ----------------------------------------------------------------------------
# 2. Desempenho no YouTube
# ----------------------------------------------------------------------------
def atualizar_desempenho(log: Log) -> list[dict]:
    import publicador

    videos = publicados()
    if not videos:
        log("Nenhum Short publicado ainda — publique alguns e volte depois de 1 dia.")
        return []
    servico = publicador._servico()
    por_id = {v["id"]: v for v in videos}
    videos = list(por_id.values())
    agora = datetime.now(timezone.utc)
    ids = list(por_id)
    for i in range(0, len(ids), 50):
        resp = servico.videos().list(part="statistics,snippet,status", id=",".join(ids[i:i + 50])).execute()
        for item in resp.get("items", []):
            v = por_id[item["id"]]
            est = item.get("statistics", {})
            v["views"] = int(est.get("viewCount", 0))
            v["likes"] = int(est.get("likeCount", 0))
            v["comentarios"] = int(est.get("commentCount", 0))
            publicado = item["snippet"].get("publishedAt")
            if item.get("status", {}).get("publishAt"):
                publicado = item["status"]["publishAt"]  # agendado: conta a partir da estreia
            inicio = datetime.fromisoformat(publicado.replace("Z", "+00:00"))
            v["horas_no_ar"] = max(0.0, round((agora - inicio).total_seconds() / 3600, 1))
            v["status"] = item.get("status", {}).get("privacyStatus")
    validos = [v for v in videos if "views" in v]
    ARQ_DESEMPENHO.write_text(json.dumps(validos, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Desempenho atualizado: {len(validos)} Shorts.")
    return validos


def _pontuacao(v: dict) -> float:
    """Visualizações (em escala log) + engajamento, para comparar Shorts de tamanhos de audiência diferentes."""
    views = v.get("views", 0)
    engaj = (v.get("likes", 0) + 3 * v.get("comentarios", 0)) / max(views, 1)
    return math.log10(views + 1) + 5 * engaj


# ----------------------------------------------------------------------------
# 3. Lições para a IA
# ----------------------------------------------------------------------------
def _linha(v: dict) -> str:
    fala = (v.get("fala") or "(fala não disponível)")[:400]
    return (f"- \"{v['titulo']}\" | {v.get('duracao', '?')}s | {v.get('views', 0)} views, "
            f"{v.get('likes', 0)} likes, {v.get('comentarios', 0)} comentários | fala: {fala}")


def gerar_licoes(op: motor.Opcoes, log: Log) -> dict | None:
    dados = json.loads(ARQ_DESEMPENHO.read_text(encoding="utf-8")) if ARQ_DESEMPENHO.exists() else []
    maduros = [v for v in dados if v.get("horas_no_ar", 0) >= IDADE_MIN_H and v.get("status") == "public"]
    if len(maduros) < MIN_SHORTS:
        log(f"Ainda não dá para tirar conclusões: {len(maduros)} Shorts públicos com 1 dia ou mais no ar "
            f"(preciso de {MIN_SHORTS}). Os outros ainda estão juntando visualizações.")
        return None
    maduros.sort(key=_pontuacao, reverse=True)
    n = max(2, min(6, len(maduros) // 3))
    melhores, piores = maduros[:n], maduros[-n:]

    sistema = ("Você analisa o desempenho de YouTube Shorts de um canal de cortes de lives de jogos e "
               "extrai regras práticas para escolher os próximos cortes e escrever os títulos. "
               "Baseie-se só nos dados; aponte padrões de conteúdo (tipo de momento, gancho inicial, duração, "
               "estilo do título), não de sorte. Escreva em português do Brasil.")
    conteudo = (f"Total analisado: {len(maduros)} Shorts.\n\nOS QUE MAIS PERFORMARAM:\n"
                + "\n".join(_linha(v) for v in melhores)
                + "\n\nOS QUE MENOS PERFORMARAM:\n" + "\n".join(_linha(v) for v in piores)
                + "\n\nCompare os dois grupos e escreva regras que ajudem a escolher cortes parecidos com os "
                  "melhores e evitar os piores.")
    resp = motor.perguntar_ia(sistema, conteudo, op, log, SCHEMA_LICOES, FORMATO_LICOES,
                              "analisar o desempenho do canal")
    licoes = {
        "atualizado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "analisados": len(maduros),
        "resumo": resp.get("resumo", ""),
        "regras": [r for r in resp.get("regras", []) if r.strip()][:10],
        "exemplos_bons": [{"titulo": v["titulo"], "views": v["views"], "fala": (v.get("fala") or "")[:300]}
                          for v in melhores[:3]],
        "exemplos_ruins": [{"titulo": v["titulo"], "views": v["views"]} for v in piores[:3]],
    }
    ARQ_LICOES.write_text(json.dumps(licoes, ensure_ascii=False, indent=2), encoding="utf-8")
    return licoes


def texto_para_prompt() -> str:
    """Bloco de instruções com o que o canal aprendeu (vazio se ainda não houver lições)."""
    if not ARQ_LICOES.exists():
        return ""
    l = json.loads(ARQ_LICOES.read_text(encoding="utf-8"))
    partes = [f"O QUE ESTE CANAL JÁ APRENDEU com {l['analisados']} Shorts publicados (siga com prioridade):",
              l.get("resumo", "")]
    partes += [f"- {r}" for r in l.get("regras", [])]
    if l.get("exemplos_bons"):
        partes.append("Exemplos de cortes que foram muito bem (busque momentos parecidos):")
        partes += [f"- \"{e['titulo']}\" ({e['views']} views): {e['fala']}" for e in l["exemplos_bons"]]
    if l.get("exemplos_ruins"):
        partes.append("Exemplos que foram mal (evite o padrão): "
                      + "; ".join(f"\"{e['titulo']}\" ({e['views']} views)" for e in l["exemplos_ruins"]))
    return "\n".join(p for p in partes if p)


def aprender(op: motor.Opcoes, log: Log) -> None:
    """Botão 'Aprender com o canal': atualiza o desempenho e gera novas lições."""
    dados = atualizar_desempenho(log)
    if dados:
        ranking = sorted(dados, key=_pontuacao, reverse=True)
        log("Ranking atual (views | likes | horas no ar):")
        for v in ranking[:10]:
            log(f"  {v['views']:>6} | {v['likes']:>4} | {v['horas_no_ar']:>5}h | {v['titulo'][:60]}")
    licoes = gerar_licoes(op, log)
    if licoes:
        log(f"🧠 Lições novas ({licoes['analisados']} Shorts analisados): {licoes['resumo']}")
        for r in licoes["regras"]:
            log(f"  • {r}")
        log("A partir de agora a IA usa essas lições para escolher os cortes e escrever os títulos.")
