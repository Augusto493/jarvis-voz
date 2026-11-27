# Jarvis — controle o Claude Code por voz

Assistente de voz que fica na bandeja do Windows, ouve você o tempo todo e controla o app **Claude desktop (aba Code)**:
abre sessões, conta como está cada projeto, manda pedidos para o Claude, aprova permissões. Responde falando, em português.

## Ilha (interface)

Uma pílula no topo da tela, no estilo da ilha do [Coucou](https://github.com/Louis-CFM/coucou) (código MIT, usado só como referência: a arquitetura de ilha fechada/aberta e o personagem em canvas cujos olhos seguem o cursor). O personagem é um orbe original; o Mochi, o nome e os sons do Coucou têm direitos reservados e não foram usados.
A ilha fica fechada (só o bonequinho e o estado). Ela abre **por clique** ou **sozinha apenas quando uma tarefa termina**; passar o mouse não abre. Aberta, mostra a conversa em bolhas, as sessões do Claude e do Orca, uma caixa para escrever ao Jarvis e os botões Pausar, Calar e Recolher. Alertas (microfone mudo, erros) aparecem só dentro dela. Ações como enviar ao Claude/Orca ou aprovar pedem confirmação (voz, texto ou botão).

O mesmo chat abre numa aba do Orca: menu da bandeja → "Abrir aba no Orca", ou peça por voz.
Arquivos: `island_ui.py` (janela WebView2) e `ui/island.html` (visual).

## Claude e Orca

O mesmo Jarvis controla o app Claude desktop (`claude_ui.py`) e o Orca (`orca_app.py`, pelo CLI `orca`): "Jarvis, como está o Jogos PG no Orca?", "abre o blockpeasant no Orca", "manda pro terminal do X: …". Quando uma tarefa do Orca termina, ele avisa igual.

## Como usar

Diga **"Ei Jarvis"** e o pedido, na mesma frase (no modo `whisper`, só "Jarvis" basta). Depois que ele responde, você tem ~10 segundos para continuar a conversa sem repetir "Jarvis".

Exemplos:

| Você diz | O que ele faz |
|---|---|
| "Jarvis, abre o Meu App" | Acha a sessão Meu App (mesmo dentro de projeto recolhido) e abre |
| "Jarvis, como está o projeto IDE IA?" | Abre a sessão, lê a conversa e te conta o andamento em linguagem natural |
| "Jarvis, o que está rodando agora?" | Diz quais sessões estão trabalhando e quais têm resposta não lida |
| "Jarvis, abre o Meu App e pede pro Claude rodar os testes" | Abre a sessão e envia o pedido |
| "Jarvis, nova sessão no Loja Virtual" | Cria uma sessão nova no projeto |
| "Jarvis, pode aprovar" / "nega" | Clica em Permitir / Negar no pedido de permissão aberto |
| "Jarvis, para" | Interrompe o Claude que está respondendo |
| "Jarvis, resume a última resposta" | Lê e resume a conversa aberta |
| "Jarvis, muda o modelo pra Sonnet" / "esforço alto" | Troca o modelo ou o esforço do chat aberto |
| "Obrigado" | Encerra a conversa |

## Quando ele fala sozinho

Só quando uma tarefa termina (uma sessão sai de "Em execução"): ele diz "Augusto, o X terminou. Você está aí?". Se você responder, ele explica o resultado ou faz o que pedir. Se não responder, fica quieto. Com o microfone mudo no Windows ele não fala nada: só mostra uma notificação.

## Uso do plano no Codenotch

O Jarvis copia a cada segundo o indicador "Uso: …%" do próprio app Claude para `%APPDATA%\codenotch\claude-live.json`, e o Codenotch usa esse número. Assim o anel do Claude acompanha o app em tempo real, sem esperar a API de uso (que recusa consultas frequentes).

## Como funciona

- **Ouvido:** microfone sempre ligado, detector de fala e **Whisper local** (`faster-whisper`, modelo `large-v3-turbo` na GPU, ~0,35 s por frase; `small` na CPU se não houver GPU). Nada de áudio sai do PC.
- **Cérebro:** um processo `claude -p` persistente (modelo **Haiku 5.5**, com memória da conversa), usando seu login do Claude Code. A resposta vem em streaming: a primeira frase já vai para a voz enquanto o resto é gerado.
- **Mãos:** `claude_ui.py` lê e clica no app Claude pela acessibilidade do Windows (UI Automation), em ~0,8 s.
- **Voz:** Piper local (`pt_BR-faber-medium`, ~0,3 s por frase). Com `"tts": "edge"` usa a voz neural Antonio da Microsoft: mais humana, mas 1 a 2 s mais lenta e precisa de internet.

## Bandeja

Ícone redondo: verde = ouvindo, amarelo = pensando, azul = falando, cinza = pausado.
Botão direito: **Pausar/Retomar**, **Abrir log**, **Sair**.

## Configuração (`config.json`, opcional)

```json
{ "brain_model": "haiku", "tts": "piper", "voice": "pt-BR-AntonioNeural", "follow_up_secs": 10,
  "end_silence_ms": 2500, "end_silence_min_ms": 1200, "wake_mode": "oww", "wake_threshold": 0.03, "barge_in": true }
```

Áudio (`audio.py`):
- **Ativação:** `"wake_mode": "oww"` usa o openWakeWord (`hey_jarvis`, CPU) sempre ligado; o Whisper só transcreve depois do disparo, e o texto precisa confirmar "Jarvis" (evita falso positivo). O modelo foi treinado em inglês: diga **"Ei Jarvis"**; só "Jarvis" quase não dispara. `"wake_mode": "whisper"` volta ao jeito antigo (transcreve toda fala e procura a palavra); é usado sozinho se o openWakeWord não carregar.
- **Fim da fala:** Silero VAD. Espera até `end_silence_ms` (2,5 s) de silêncio; se a transcrição parcial já parecer frase completa (termina em ponto/interrogação ou é comando curto), responde com `end_silence_min_ms` (1,2 s). Frase com pausa no meio ("abre o… Meu App") espera o máximo.
- **Interromper:** fale por cima da voz dele por ~0,35 s (`barge_in_ms`) e ele se cala e ouve o novo pedido; o eco da caixa é comparado com o que está tocando para ele não se interromper sozinho. "Para"/"chega" por cima só cala.
- **Saída/entrada:** usa o Mapeador de som do Windows (segue a troca de fone/caixa); se o fluxo falhar, tenta de novo de onde parou, reamostra se a taxa for recusada e reinicia o PortAudio como último recurso.

`brain_model` aceita `sonnet` para respostas melhores (um pouco mais lento). `voice` aceita `pt-BR-FranciscaNeural` (voz feminina).

## Início automático

Registrado em `HKCU\...\Run` como `JarvisClaude` (inicia sem a saudação). Para remover:

```
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v JarvisClaude /f
```

## Teste sem microfone

```
py -3.12 jarvis.py --say "Jarvis, como está o projeto Meu App?"
```

## Problemas

- **Ele não me ouve:** o microfone do Windows precisa estar ativo (tecla de microfone do notebook ou Configurações > Sistema > Som > Entrada). Se o microfone estiver mudo, o Jarvis avisa falando.
- **Não acha uma sessão:** diga o nome como aparece na barra lateral. O log (`jarvis.log`) mostra o que ele ouviu e o que fez.

## Ilha no layout do Coucou (atualização)
- Fechada: ilha preta 184x32 com um ponto; compacta (288x32) ao passar o mouse; aberta (640) só por clique, por tarefa concluída ou por pedido de permissão.
- Aberta: abas (visão geral / chat / nova sessão), cartão esquerdo com o personagem + sessão em andamento, cartão direito com as sessões do Claude e do Orca (clique abre a sessão), cartão **Negar / Permitir** para permissões do Claude, chat e criação de sessão.
- O personagem é original (o Mochi e o nome são de uso reservado do autor do Coucou). Código base: Louis-CFM/coucou, `windows/src/style.css` (MIT).
- Permissões do Orca: aparecem como pílula laranja (esperando); o botão abre o workspace, não aprova sozinho.
