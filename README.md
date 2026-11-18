# Máquina de Cortes

Pega um vídeo longo (arquivo do PC ou link), acha os melhores momentos com IA e
entrega cortes prontos para TikTok/Reels/Shorts.

## Como usar
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
- **em cima:** só o rosto do streamer, recortado da webcam e centralizado;
- **no meio:** o título numa faixa vermelha;
- **embaixo:** o jogo inteiro (sem cortar nada), com fundo desfocado e a legenda no espaço que sobra.

O rosto é achado sozinho. Se o recorte não ficar bom, clique em **Marcar webcam...**, arraste um
retângulo em volta do rosto e salve — fica guardado para as próximas lives (o botão **Automática** desfaz).
Para vídeos sem webcam, troque o layout para **Normal**.

## Live ao vivo
Cole o link de uma live **que está acontecendo** (YouTube, Twitch...). O programa percebe que é ao vivo,
grava em blocos (padrão: 30 min, ajuste em "Live: cortar a cada") e solta os cortes de cada bloco
assim que ele fecha. Clique em **Parar live** para encerrar — o último bloco ainda é cortado.
Se a conexão cair no meio, ele reconecta sozinho enquanto a live estiver no ar.
Os cortes saem como `b01-01 ...`, `b02-01 ...` (bloco - número do corte).
Obs.: um momento que fica bem na divisa entre dois blocos pode ser perdido.

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
