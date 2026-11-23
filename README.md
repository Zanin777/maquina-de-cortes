# Máquina de Cortes

<p align="center">
  <a href="https://github.com/Zanin777/maquina-de-cortes/releases/latest/download/MaquinaDeCortes.exe">
    <img src="https://img.shields.io/badge/⬇%20Baixar-MaquinaDeCortes.exe-E01A1A?style=for-the-badge&logo=windows&logoColor=white" alt="Baixar MaquinaDeCortes.exe">
  </a>
</p>

**Para usar:** clique no botão acima, abra o `MaquinaDeCortes.exe` e pronto — não precisa instalar Python.
- Na 1ª vez, se faltar o FFmpeg, o programa oferece instalar sozinho.
- Para a IA: use uma chave da API **ou** o Claude Code logado na sua conta Pro/Max (veja abaixo).
- O Windows pode mostrar "O Windows protegeu o computador" (o .exe não é assinado): clique em
  **Mais informações → Executar assim mesmo**.
- Os cortes ficam em `Documentos\MaquinaCortes\cortes`.

Pega um vídeo longo (arquivo do PC ou link), acha os melhores momentos com IA e
entrega cortes prontos para TikTok/Reels/Shorts.

## Rodar pelo código-fonte (opcional)
Para quem prefere rodar o código em vez do .exe (para gerar o .exe: `gerar_exe.bat`):
1. Dê dois cliques em **iniciar.bat** (na 1ª vez ele instala tudo, demora alguns minutos).
2. Cole um link ou clique em **Escolher arquivo...**
3. Escolha como usar a IA (veja abaixo).
4. Clique em **Fazer cortes**.

## IA: assinatura Pro ou chave de API
- **Assinatura Claude Pro/Max (sem custo extra):** instale o Claude Code e faça login uma vez
  com sua conta. Deixe o campo da chave **vazio** e o programa usa o Claude Code.
  ```powershell
  irm https://claude.ai/install.ps1 | iex
  claude
  ```
  (conta nos limites de uso da sua assinatura)

  Se você já tem o app Claude para desktop, ele traz uma cópia do Claude Code que o programa
  também encontra — mas ela precisa de um login próprio: rode o `claude.exe` dela uma vez e digite `/login`.
- **Chave de API:** crie em https://console.anthropic.com (cobrança separada, por uso) e cole na janela.
  Fica salva em `config.json`, que não vai pro GitHub.

## Layout streamer (padrão)
Para lives de jogo, cada corte sai assim:
- **em cima (40% da tela):** só o rosto do streamer, recortado da webcam e centralizado;
- **embaixo (60%):** o jogo preenchendo tudo, recortado para deixar de fora a webcam da live (o rosto não
  aparece duas vezes; se ela ficar dentro do recorte, aquele pedaço é desfocado), com a legenda por cima;
- **nos primeiros 2,5 s:** o gancho (a pergunta do começo) numa faixa vermelha entre os dois, que depois some.

O rosto é achado sozinho. Se o recorte não ficar bom, clique em **Marcar webcam...**, arraste um
retângulo em volta do rosto e salve — fica guardado para as próximas lives (o botão **Automática** desfaz).
Para vídeos sem webcam, troque o layout para **Normal**.

## Publicar no YouTube Shorts automaticamente
Cada corte pronto pode ir direto pro seu canal (título, descrição e #shorts já vão junto).
O login é feito por você, no navegador, numa tela do Google — o programa **nunca vê sua senha**,
só guarda uma autorização no seu PC (`Documentos\MaquinaCortes\youtube_token.json`).

**Configuração (uma vez só, grátis):**
1. Entre em https://console.cloud.google.com com a conta do canal e crie um projeto (ex.: "Maquina de Cortes").
2. Em **APIs e serviços → Biblioteca**, procure **YouTube Data API v3** e clique em **Ativar**.
3. Em **Tela de permissão OAuth** (ou "Google Auth Platform"): tipo **Externo**, preencha nome e e-mail,
   e em **Usuários de teste** adicione o e-mail da conta do canal.
4. Em **Credenciais → Criar credenciais → ID do cliente OAuth**, tipo **App para computador**. Baixe o JSON.
5. Na Máquina de Cortes, clique em **Conectar YouTube...**, escolha esse JSON e faça login no navegador.
   Vai aparecer um aviso de "app não verificado" (é o seu próprio app): **Avançado → Acessar**.

**Revisar antes (ligado por padrão):** nada sobe sozinho. Quando os cortes ficam prontos abre a tela de
revisão: assista cada corte (**▶ Ver**), troque o título ou ajuste início e fim e clique em **Refazer**
(o vídeo é editado de novo na hora), e marque os que vão para o canal. **Publicar aprovados** envia só
esses; os desmarcados ficam como descartados e nunca são publicados (nem pela fila do limite diário).
Dá para abrir essa tela a qualquer hora em **✔ Revisar cortes...** (inclusive no meio de uma live).
Desmarque "Revisar antes" para voltar a publicar direto, sem revisão.

**Opções na janela:**
- **Público na hora**, **Agendado (fila)** — um Short a cada X horas, para não soltar tudo de uma vez —,
  **Não listado** (você revisa antes) ou **Privado**.
- **Nota mínima:** só publica cortes que a IA avaliou com nota igual ou maior.
- O link de cada Short aparece no registro e fica salvo em `cortes.json`.

Obs.: com o app em modo de teste, o Google pode pedir para entrar de novo a cada 7 dias.
Publique só conteúdo que você tem direito de usar (cortes de lives de terceiros precisam de autorização
do streamer, senão o canal pode levar reivindicação de direitos autorais ou strike).

## Live ao vivo
Cole o link de uma live **que está acontecendo** (YouTube, Twitch...). O programa percebe que é ao vivo,
grava em blocos (padrão: 30 min, ajuste em "Live: cortar a cada") e solta os cortes de cada bloco
assim que ele fecha. Clique em **Parar live** para encerrar — o último bloco ainda é cortado.
Se a conexão cair no meio, ele reconecta sozinho enquanto a live estiver no ar.
Os cortes saem como `b01-01 ...`, `b02-01 ...` (bloco - número do corte).
Cada bloco é cortado junto com os 2 últimos minutos do anterior, então um momento bem na divisa
entre dois blocos não se perde (e nada que já virou corte é repetido).

Os cortes saem em `cortes\<nome do vídeo>\`, junto com `legendas_dos_posts.txt`
(título + descrição com hashtags de cada corte, pra colar no post).

## O que acontece por dentro
1. **Download** (se for link) com yt-dlp → pasta `downloads\`
2. **Transcrição** com Whisper no seu PC (fica salva, então rodar de novo o mesmo vídeo é rápido)
3. **IA (Claude)** lê a transcrição e escolhe os trechos com mais potencial, com título e descrição
4. **Edição** com FFmpeg: recorte 9:16 centralizado no rosto, zoom lento, título no topo e
   legenda palavra por palavra (a palavra falada fica amarela)

## Dicas
- **Precisão da transcrição**: `small` é o equilíbrio. `medium`/`large-v3` erram menos, mas são bem mais lentos sem placa NVIDIA.
- **Instruções para a IA**: ex. "foque nos momentos engraçados" ou "só cortes sobre investimentos".
- Custo da IA: centavos por vídeo (só a transcrição em texto é enviada, não o vídeo).
- Use com vídeos seus ou que você tem permissão de usar.
