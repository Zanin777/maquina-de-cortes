"""Autoteste das partes do motor que não dependem de IA nem de vídeo: python testes.py"""
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


if __name__ == "__main__":
    for nome, f in list(globals().items()):
        if nome.startswith("teste_"):
            f()
            print("ok", nome)
