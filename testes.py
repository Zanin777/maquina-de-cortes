"""Autoteste das partes do motor que não dependem de IA nem de vídeo: python testes.py"""
import json
import tempfile
from pathlib import Path

import motor


def teste_picos():
    niveis = [-40.0] * 100
    niveis[50] = niveis[51] = -10.0  # grito de 1 s
    niveis[80] = -12.0
    picos = motor._achar_picos(niveis, 0.5)
    assert [p["inicio"] for p in picos] == [25.0, 40.0], picos
    assert picos[0]["fim"] == 26.0 and picos[0]["db"] == 30.0
    assert motor._achar_picos([-40.0] * 100, 0.5) == []  # volume constante: nenhum pico
    segs = [{"inicio": 24.0, "fim": 27.0, "texto": "socorro"}]
    txt = motor._transcricao_com_picos(segs, picos)
    assert "socorro [🔊 volume alto +30 dB]" in txt and "sem fala" in txt, txt


def teste_alucinacoes():
    assert motor.eh_alucinacao("Legendas pela comunidade Amara.org")
    assert motor.eh_alucinacao(" Obrigado por assistir!")
    assert motor.eh_alucinacao("...")
    assert not motor.eh_alucinacao("Cara, o chefe me matou de novo")
    # fala longa de verdade que só menciona a frase continua
    assert not motor.eh_alucinacao("e aí eu falei pra ele: obrigado por assistir a live inteira, você é brabo demais mano")


def teste_encurtar_pelo_comeco():
    P = motor.Palavra
    palavras = [P(t, t + 0.8, f"w{t}") for t in range(0, 70)]  # 70 s de fala
    # frases começam a cada 10 s: tira 10 s do começo e mantém o final (punchline)
    resto, onde = motor._caber_na_duracao(palavras, [0, 10, 20, 30, 40, 50, 60], 59)
    assert onde == "começo" and resto[0].inicio == 20 and resto[-1].inicio == 69, (onde, resto[0])
    # sem início de frase que sirva (só lá no fim): corta o fim, como antes
    resto, onde = motor._caber_na_duracao(palavras, [0, 65], 59)
    assert onde == "fim" and resto[0].inicio == 0 and resto[-1].fim <= 58.4
    # já cabe: não mexe
    assert motor._caber_na_duracao(palavras[:30], [0, 10], 59) == (palavras[:30], "")


def teste_publicar_respeita_revisao():
    import publicador
    enviados = []
    antigos = publicador.publicar, publicador.registrar, publicador.publicados
    publicador.publicar = lambda video, *a, **k: enviados.append(video.name) or "https://youtube.com/shorts/x"
    publicador.registrar = lambda *a: None
    publicador.publicados = lambda: {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            cortes = [{"arquivo": "bom.mp4", "titulo": "a", "nota": 9},
                      {"arquivo": "descartado.mp4", "titulo": "b", "nota": 9, "descartado": True},
                      {"arquivo": "nota-baixa-aprovado.mp4", "titulo": "c", "nota": 3, "aprovado": True},
                      {"arquivo": "nota-baixa.mp4", "titulo": "d", "nota": 3}]
            for c in cortes:
                (pasta / c["arquivo"]).write_bytes(b"x")
            (pasta / "cortes.json").write_text(json.dumps(cortes), encoding="utf-8")
            publicador.publicar_pasta(pasta, "privado", 20, 7, lambda m: None)
    finally:
        publicador.publicar, publicador.registrar, publicador.publicados = antigos
    assert sorted(enviados) == ["bom.mp4", "nota-baixa-aprovado.mp4"], enviados


if __name__ == "__main__":
    for nome, f in list(globals().items()):
        if nome.startswith("teste_"):
            f()
            print("ok", nome)
