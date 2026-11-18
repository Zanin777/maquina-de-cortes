# Máquina de Cortes

Pega um vídeo longo (arquivo do PC ou link), acha os melhores momentos com IA e
entrega cortes prontos para TikTok/Reels/Shorts.

## Como usar
1. Dê dois cliques em **iniciar.bat** (na 1ª vez ele instala tudo, demora alguns minutos).
2. Cole um link ou clique em **Escolher arquivo...**
3. Cole sua chave da API Claude (crie em https://console.anthropic.com). Ela fica salva em `config.json`.
4. Clique em **Fazer cortes**.

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
