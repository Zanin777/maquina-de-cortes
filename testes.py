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


def teste_comecar_perto_do_pico():
    P = motor.Palavra
    palavras = [P(t, t + 0.8, f"w{t}") for t in range(0, 40)]  # 40 s de fala
    frases = [0, 10, 18, 23, 30]
    # pico aos 25 s: começa na frase dos 23 s (dentro dos 4 s antes do pico), não no menu lá do 0
    resto = motor._comecar_perto_do_pico(palavras, frases, 25.0)
    assert resto[0].inicio == 23 and resto[-1].inicio == 39, resto[0]
    # sem início de frase na janela: começa na 1ª palavra que fica até 4 s antes do pico
    resto = motor._comecar_perto_do_pico(palavras, [0], 25.0)
    assert resto[0].inicio == 21, resto[0]
    # pico já perto do começo, pico fora do corte ou sem pico: não mexe
    assert motor._comecar_perto_do_pico(palavras, frases, 3.0) == palavras
    assert motor._comecar_perto_do_pico(palavras, frases, 99.0) == palavras
    assert motor._comecar_perto_do_pico(palavras, frases, None) == palavras
    # sobraria menos de 6 s depois do pico: não mexe (o corte ficaria curto demais)
    assert motor._comecar_perto_do_pico(palavras, frases + [36], 38.0) == palavras
    # com mínimo de 10,5 s (70% de 15 s): pico aos 33 s deixaria só ~9 s, então não mexe
    assert motor._comecar_perto_do_pico(palavras, frases, 33.0)[0].inicio == 30
    assert motor._comecar_perto_do_pico(palavras, frases, 33.0, minimo=10.5) == palavras


def teste_tirar_pausas():
    P = motor.Palavra
    # corte de 30 s numa live de jogo
    palavras = [P(0.0, 0.5, "a"), P(0.6, 1.0, "b"),
                P(2.0, 2.5, "c"),     # pausa de 1 s na fala: sai
                P(10.0, 10.5, "d"),   # 7,5 s quieto = jogando/vendo a cena: fica
                P(11.5, 12.0, "e"),   # pausa de 1 s com explosão no jogo: fica
                P(13.5, 14.0, "f"),   # pausa de 1,5 s: sai
                P(29.0, 29.5, "g")]   # 15 s de gameplay: fica
    picos = [{"inicio": 10.8, "fim": 11.2, "db": 20}]
    trechos = motor.trechos_sem_pausa(palavras, 0.0, 30.0, picos)
    assert trechos == [(0.0, 1.12), (1.88, 12.12), (13.38, 30.0)], trechos
    tirado = 30 - sum(b - a for a, b in trechos)
    assert abs(tirado - 2.02) < 1e-9, tirado  # a gameplay quieta não some (antes sumiam 22 s daqui)
    # nunca tira mais de 20%: corte de 5 s com duas pausas de 1 s -> só uma sai
    curtas = [P(0.0, 0.5, "a"), P(1.5, 2.0, "b"), P(3.0, 3.5, "c")]
    assert motor.trechos_sem_pausa(curtas, 0.0, 5.0) == [(0.0, 0.62), (1.38, 5.0)]
    # tempos do original no corte já sem pausas
    assert motor._mapear_tempo(0.5, trechos) == 0.5
    assert abs(motor._mapear_tempo(2.0, trechos) - (1.12 + 0.12)) < 1e-9
    assert motor._mapear_tempo(1.5, trechos) == motor._mapear_tempo(1.88, trechos)  # dentro da pausa
    assert abs(motor._mapear_tempo(99, trechos) - sum(b - a for a, b in trechos)) < 1e-9


def teste_gancho_no_comeco():
    c = motor.Corte(100, 120, "Título do corte", "", 8, [motor.Palavra(100.2, 100.6, "oi")],
                    gancho="espera o que acontece")
    linhas = motor.gerar_ass(c, 1080, 1920, motor.Opcoes(), 768, titulo_vermelho=True).splitlines()
    gancho = [l for l in linhas if "ESPERA O QUE ACONTECE" in l]
    assert len(gancho) == 1 and gancho[0].split(",")[1:3] == ["0:00:00.00", "0:00:02.50"], gancho
    assert not [l for l in linhas if "TÍTULO DO CORTE" in l]  # a faixa não fica parada repetindo o título
    assert all(l.split(",")[2] <= "0:00:02.50" for l in linhas if "Desenho" in l and l.startswith("Dialogue"))
    # sem gancho: o título aparece só no começo
    c.gancho = ""
    linhas = motor.gerar_ass(c, 1080, 1920, motor.Opcoes(), 768, titulo_vermelho=True).splitlines()
    titulo = [l for l in linhas if "TÍTULO DO CORTE" in l]
    assert titulo[0].split(",")[1:3] == ["0:00:00.00", "0:00:02.50"], titulo


def teste_jogo_foge_da_webcam():
    # quadro 1920 de largura, recorte do jogo de 1012: webcam no canto esquerdo -> recorte vai pra direita
    # (tolerância de até 10 px de webcam na borda do recorte)
    x = motor._x_do_jogo(1012, 1920, (0, 0, 500, 400))
    assert x >= 490, x
    # webcam no canto direito -> recorte vai pra esquerda
    x = motor._x_do_jogo(1012, 1920, (1500, 600, 1920, 1080))
    assert x + 1012 <= 1510, x
    # webcam longe do meio -> fica no meio
    assert motor._x_do_jogo(1012, 1920, (0, 0, 200, 200)) == (1920 - 1012) // 2


def teste_deslocar_para_o_trecho():
    P = motor.Palavra
    c = motor.Corte(100.0, 130.0, "t", "", 8, [P(100.2, 100.6, "oi"), P(129.0, 129.5, "fim")], pico=110.0)
    d = motor._deslocar(c, 97.0)  # trecho baixado começa 3 s antes do corte
    assert (d.inicio, d.fim, d.pico) == (3.0, 33.0, 13.0)
    assert [(round(w.inicio, 2), round(w.fim, 2)) for w in d.palavras] == [(3.2, 3.6), (32.0, 32.5)]
    assert c.inicio == 100.0 and c.palavras[0].inicio == 100.2  # o original não muda


def teste_quantidade_exata_de_cortes():
    # 10 min de fala, uma palavra por segundo, uma frase a cada 5 s
    segmentos = [{"inicio": float(t), "fim": t + 5.0, "texto": "x",
                  "palavras": [{"inicio": t + i + 0.1, "fim": t + i + 0.8, "texto": "w"} for i in range(5)]}
                 for t in range(0, 600, 5)]

    def corte(ini, fim, titulo):
        return {"inicio": ini, "fim": fim, "pico": ini + 1, "titulo": titulo, "gancho": "", "descricao": "",
                "nota": 8, "tags": []}
    respostas = [
        {"cortes": [corte(10, 35, "a"), corte(100, 104, "curto demais"), corte(20, 45, "sobrepõe o a"),
                    corte(200, 225, "b")]},
        {"cortes": [corte(300, 325, "c"), corte(400, 425, "d")]},
    ]
    pedidos = []

    def ia_falsa(sistema, conteudo, *a, **k):
        pedidos.append(conteudo)
        return respostas[len(pedidos) - 1]
    antiga = motor.perguntar_ia
    motor.perguntar_ia = ia_falsa
    try:
        op = motor.Opcoes(qtd_cortes=3, dur_min=15, dur_max=35)
        cortes = motor.escolher_cortes(segmentos, op, lambda m: None, exatos=True)
        assert [c.titulo for c in cortes] == ["a", "b", "c"], [c.titulo for c in cortes]  # exatamente 3
        assert len(pedidos) == 2 and "os 5 melhores cortes" in pedidos[0]
        assert "já viraram corte" in pedidos[1] and "(a)" in pedidos[1] and "(b)" in pedidos[1]
        # live: "até" a quantidade, numa rodada só
        pedidos.clear()
        cortes = motor.escolher_cortes(segmentos, op, lambda m: None, exatos=False)
        assert [c.titulo for c in cortes] == ["a", "b"] and len(pedidos) == 1 and "até 3" in pedidos[0]
    finally:
        motor.perguntar_ia = antiga


def teste_corte_sem_acao_nao_sai():
    segmentos = [{"inicio": float(t), "fim": t + 5.0, "texto": "x",
                  "palavras": [{"inicio": t + i + 0.1, "fim": t + i + 0.8, "texto": "w"} for i in range(5)]}
                 for t in range(0, 600, 5)]

    def corte(ini, titulo, tem_acao=True, nota=8):
        return {"inicio": ini, "fim": ini + 25, "pico": ini + 1, "titulo": titulo, "gancho": "", "descricao": "",
                "nota": nota, "tags": [], "acontece": "algo", "tem_acao": tem_acao}
    respostas = [{"cortes": [corte(10, "fuga", True), corte(100, "opinião sobre o carro", False),
                             corte(200, "nota baixa", True, nota=3), corte(300, "tiroteio", True)]},
                 {"cortes": []}]  # 2ª rodada: a IA não acha mais nada bom
    pedidos = []

    def ia_falsa(sistema, conteudo, *a, **k):
        pedidos.append(conteudo)
        return respostas[len(pedidos) - 1]
    antiga = motor.perguntar_ia
    motor.perguntar_ia = ia_falsa
    avisos = []
    try:
        op = motor.Opcoes(qtd_cortes=4, dur_min=15, dur_max=35)
        cortes = motor.escolher_cortes(segmentos, op, avisos.append, exatos=True)
    finally:
        motor.perguntar_ia = antiga
    # só os 2 com ação e nota boa saem; não enche com o resto e para de pedir quando a IA não acha mais
    assert [c.titulo for c in cortes] == ["fuga", "tiroteio"], [c.titulo for c in cortes]
    assert len(pedidos) == 2 and cortes[0].acontece == "algo"
    assert any("nada acontecendo" in a for a in avisos) and any("só tem 2 momentos" in a for a in avisos)


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


def teste_pasta_livre():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "video"
        assert motor._pasta_livre(base) == base  # não existe: usa ela
        base.mkdir()
        assert motor._pasta_livre(base) == base  # existe mas vazia: usa ela
        (base / "01 - a.mp4").write_bytes(b"x")
        assert motor._pasta_livre(base).name == "video (2)"
        (Path(tmp) / "video (2)").mkdir()
        (Path(tmp) / "video (2)" / "cortes.json").write_text("[]")
        assert motor._pasta_livre(base).name == "video (3)"


def teste_login_vencido_nao_fica_tentando():
    chamadas = []

    def falha(*a):
        chamadas.append(1)
        raise RuntimeError("Claude Code: Failed to authenticate: OAuth session expired")
    antigo = motor._ia_pelo_claude_code
    motor._ia_pelo_claude_code = falha
    try:
        motor.perguntar_ia("s", "c", motor.Opcoes(), lambda m: None, {}, "", "t")
    except RuntimeError as e:
        assert "/login" in str(e)
    else:
        raise AssertionError("devia falhar")
    finally:
        motor._ia_pelo_claude_code = antigo
    assert len(chamadas) == 1  # sem esperar 7 min em tentativas inúteis


if __name__ == "__main__":
    for nome, f in list(globals().items()):
        if nome.startswith("teste_"):
            f()
            print("ok", nome)
