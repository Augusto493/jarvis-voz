# Jarvis — controle o Claude Code por voz

**English:** a Windows tray voice assistant (Brazilian Portuguese) that drives the Claude Code desktop app: open sessions, hear project status, send prompts and approve permissions by voice. Early stage; contributions welcome.

Assistente de voz que fica na bandeja do Windows, ouve você o tempo todo e controla o app **Claude desktop (aba Code)**:
abre sessões, conta como está cada projeto, manda pedidos para o Claude, aprova permissões. Responde falando, em português.

## Ilha (interface)

Uma pílula no topo da tela, no estilo da ilha do [Coucou](https://github.com/Louis-CFM/coucou) (código MIT; o layout e as animações da ilha derivam dele, ver [avisos de terceiros](THIRD_PARTY_NOTICES.md)). O personagem é um orbe original; o Mochi, o nome e os sons do Coucou têm direitos reservados e não foram usados.
A ilha fica fechada (uma linha com o estado). Passar o mouse mostra só a versão compacta, sem abrir. Ela abre inteira **por clique** ou **sozinha** quando uma tarefa termina, quando o Claude pede permissão ou quando o Jarvis pede uma confirmação. Aberta, mostra a conversa em bolhas, as sessões do Claude e do Orca, uma caixa para escrever ao Jarvis e os botões Pausar, Calar e Recolher. Alertas (microfone mudo, erros) aparecem só dentro dela. Enviar ao Claude/Orca, aprovar e negar, quando pedidos ao Jarvis, exigem [confirmação](#confirmação-de-ações).

O mesmo chat abre numa aba do Orca: menu da bandeja → "Abrir aba no Orca", ou peça por voz.
Arquivos: `island_ui.py` (janela WebView2) e `ui/island.html` (visual).

## Claude e Orca

O mesmo Jarvis controla o app Claude desktop (`claude_ui.py`) e o Orca (`orca_app.py`, pelo CLI `orca`): "Jarvis, como está a Loja Virtual no Orca?", "abre o Meu App no Orca", "manda pro terminal do X: …". Quando uma tarefa do Orca termina, ele avisa igual.

## Como usar

Diga **"Ei Jarvis"** e o pedido, na mesma frase (no modo `whisper`, só "Jarvis" basta). Depois que ele responde, você tem ~10 segundos para continuar a conversa sem repetir "Jarvis".

Exemplos:

| Você diz | O que ele faz |
|---|---|
| "Jarvis, abre o Meu App" | Acha a sessão Meu App (mesmo dentro de projeto recolhido) e abre |
| "Jarvis, como está o projeto Blog Pessoal?" | Abre a sessão, lê a conversa e te conta o andamento em linguagem natural |
| "Jarvis, o que está rodando agora?" | Diz quais sessões estão trabalhando e quais têm resposta não lida |
| "Jarvis, abre o Meu App e pede pro Claude rodar os testes" | Abre a sessão e envia o pedido |
| "Jarvis, nova sessão no Loja Virtual" | Cria uma sessão nova no projeto |
| "Jarvis, pode aprovar" / "nega" | Clica em Permitir / Negar no pedido de permissão aberto |
| "Jarvis, para" | Interrompe o Claude que está respondendo |
| "Jarvis, resume a última resposta" | Lê e resume a conversa aberta |
| "Jarvis, muda o modelo pra Sonnet" / "esforço alto" | Troca o modelo ou o esforço do chat aberto |
| "Obrigado" | Encerra a conversa |

## Confirmação de ações

O microfone fica sempre aberto, então palavras soltas como "ok", "claro", "isso" ou "sim" **não confirmam nada**. Para liberar:

- **voz:** a frase inteira "Jarvis, confirmo" (também "Jarvis, sim", "Jarvis, pode mandar", "Ei Jarvis, confirma") ou "confirmo a ação", depois que ele terminar de perguntar;
- **texto na ilha:** "sim", "confirmo" ou "pode";
- **botão** Sim da ilha.

Negação ("não", "cancela", "para", "deixa"…) sempre vence, mesmo no meio de outra frase. Sem resposta em 25 s, nada é feito. As regras estão em `confirm.py`.

Antes de aprovar uma permissão do Claude, o Jarvis diz e mostra o botão que vai clicar e o texto do pedido lido na tela. Se não conseguir identificar a ferramenta ou o comando, ele avisa; confira no app Claude antes de confirmar. A leitura depende do texto do cartão do app ("Permitir que o Claude…", "Allow Claude to…") e ainda não foi validada contra o app real. Se o pedido lido mudar durante a confirmação, nada é clicado.

## Quando ele fala sozinho

Só quando uma tarefa termina (uma sessão sai de "Em execução"): ele diz "O X terminou. Você está aí?" (com `user_name` configurado: "Nome, o X terminou…"). Se você responder, ele explica o resultado ou faz o que pedir. Se não responder, fica quieto. Com o microfone mudo no Windows ele não fala nada: só mostra uma notificação.

## Uso do plano no Codenotch

O Jarvis copia a cada segundo o indicador "Uso: …%" do próprio app Claude para `%APPDATA%\codenotch\claude-live.json`, e o Codenotch usa esse número. Assim o anel do Claude acompanha o app em tempo real, sem esperar a API de uso (que recusa consultas frequentes).

## Como funciona

- **Ouvido:** microfone sempre ligado, detector de fala e **Whisper local** (`faster-whisper`, modelo `large-v3-turbo` na GPU, ~0,35 s por frase; `small` na CPU se não houver GPU). Nada de áudio sai do PC.
- **Cérebro:** um processo `claude -p` persistente, definido no próprio `jarvis.py` (modelo `haiku` por padrão, com memória da conversa), usando seu login do Claude Code. A resposta vem em streaming: a primeira frase já vai para a voz enquanto o resto é gerado.
- **Mãos:** `claude_ui.py` lê e clica no app Claude pela acessibilidade do Windows (UI Automation), em ~0,8 s.
- **Voz:** Piper local (`pt_BR-faber-medium`, ~0,3 s por frase). Com `"tts": "edge"` usa a voz neural Antonio da Microsoft: mais humana, mas 1 a 2 s mais lenta e precisa de internet.

## Bandeja

Ícone redondo: verde = ouvindo, amarelo = pensando, azul = falando, cinza = pausado.
Vermelho = microfone mudo ou carregando. Botão direito: **Pausar/Retomar**, **Abrir aba no Orca**, **Abrir log**, **Sair**.

## Configuração (`config.json`, opcional)

```json
{ "brain_model": "haiku", "tts": "piper", "voice": "pt-BR-AntonioNeural", "follow_up_secs": 10,
  "end_silence_ms": 2500, "end_silence_min_ms": 1200, "wake_mode": "oww", "wake_threshold": 0.03, "barge_in": true,
  "user_name": "" }
```

Áudio (`audio.py`):
- **Ativação:** `"wake_mode": "oww"` usa o openWakeWord (`hey_jarvis`, CPU) sempre ligado; o Whisper só transcreve depois do disparo, e o texto precisa confirmar "Jarvis" (evita falso positivo). O modelo foi treinado em inglês: diga **"Ei Jarvis"**; só "Jarvis" quase não dispara. `"wake_mode": "whisper"` volta ao jeito antigo (transcreve toda fala e procura a palavra); é usado sozinho se o openWakeWord não carregar.
- **Fim da fala:** Silero VAD. Espera até `end_silence_ms` (2,5 s) de silêncio; se a transcrição parcial já parecer frase completa (termina em ponto/interrogação ou é comando curto), responde com `end_silence_min_ms` (1,2 s). Frase com pausa no meio ("abre o… Meu App") espera o máximo.
- **Interromper:** fale por cima da voz dele por ~0,35 s (`barge_in_ms`) e ele se cala e ouve o novo pedido; o eco da caixa é comparado com o que está tocando para ele não se interromper sozinho. "Para"/"chega" por cima só cala.
- **Saída/entrada:** usa o Mapeador de som do Windows (segue a troca de fone/caixa); se o fluxo falhar, tenta de novo de onde parou, reamostra se a taxa for recusada e reinicia o PortAudio como último recurso.

`user_name` é como o Jarvis pode te chamar; vazio (padrão) = sem nome. O `config.json` do repositório usa `"tts": "piper"`; **sem a chave `tts`, o código usa o edge-tts** (online, Microsoft).

`brain_model` aceita `sonnet` para respostas melhores (um pouco mais lento). `voice` aceita `pt-BR-FranciscaNeural` (voz feminina).

## Início automático

O projeto **não** se registra para iniciar com o Windows: não há código que grave no registro. Se quiser, crie por conta própria um atalho para `pyw -3.12 jarvis.py` na pasta `shell:startup`.

## Teste sem microfone

```
py -3.12 jarvis.py --say "Jarvis, como está o projeto Meu App?"
```

## Problemas

- **Ele não me ouve:** o microfone do Windows precisa estar ativo (tecla de microfone do notebook ou Configurações > Sistema > Som > Entrada). Se o microfone estiver mudo, o Jarvis avisa por notificação e dentro da ilha.
- **Não acha uma sessão:** diga o nome como aparece na barra lateral. O log (`jarvis.log`) mostra o que ele ouviu e o que fez (veja [Privacidade](#privacidade)).

## Ilha no layout do Coucou (atualização)
- Fechada: ilha preta 184x32 com o indicador de estado; compacta (288x32) ao passar o mouse; aberta (640) por clique, tarefa concluída, pedido de permissão ou confirmação.
- Aberta: abas (visão geral / chat / nova sessão), cartão esquerdo com o personagem + sessão em andamento, cartão direito com as sessões do Claude e do Orca (clique abre a sessão), cartão **Negar / Permitir** para permissões do Claude, chat e criação de sessão.
- O personagem é original (o Mochi e o nome são de uso reservado do autor do Coucou). Código base: Louis-CFM/coucou, `windows/src/style.css` (MIT).
- Permissões do Orca: aparecem como pílula laranja (esperando); o botão abre o workspace, não aprova sozinho.

## Instalação (Windows, Python 3.12)

```
py -3.12 -m pip install -r requirements.txt
```

O arquivo da voz Piper não vem no repositório: baixe `pt_BR-faber-medium.onnx` e o `.onnx.json` do projeto Piper Voices e coloque em `voices/` (ou use `"tts": "edge"` no `config.json`). Confira a licença da voz antes de redistribuir. A lista de dependências foi montada a partir dos imports e ainda não foi testada numa instalação limpa.

## Módulos experimentais (não ligados ao app)

`brain.py` (cérebro com ferramentas MCP), `jarvis_mcp.py` (servidor MCP, inclui memória no Obsidian) e `hooks_bridge.py` + `jarvis_hook.py` (ganchos do Claude Code) **não são importados pelo `jarvis.py`**. O app usa o próprio cérebro, lê o Claude pela tela e não atende `/rpc/tool` nem `/rpc/hook`. Eles têm testes, mas nada do que descrevem (ferramentas MCP, memória, permissões por gancho) funciona no app hoje. Não rode `hooks_bridge.py --install` para uso normal: ele altera `~/.claude/settings.json` sem efeito no app.

## Privacidade

- **Áudio:** o reconhecimento de fala é local; o código não envia áudio para fora. Na primeira execução, as bibliotecas podem baixar modelos da internet.
- **Log:** `jarvis.log`, na pasta do projeto, grava em texto puro as transcrições (inclusive parciais e falas captadas que não eram para ele), o que você digita, as respostas e falas do Jarvis, os resultados das ações (podem conter trechos das conversas do Claude e do Orca), notificações e o texto dos pedidos de permissão. Não há rotação nem limpeza automática: apague quando quiser e não compartilhe sem revisar.
- **Anthropic:** pelo `claude -p`, são enviados o texto do seu pedido e o contexto (nomes de sessões, projetos e workspaces, tarefas, modelo e o fim da conversa aberta no Claude) e os resultados das ações (como conversas lidas e o último pedido e resposta do agente no Orca). Valem os termos da sua conta.
- **Microsoft:** com `"tts": "edge"` (ou sem a chave `tts`), todo texto que o Jarvis fala vai para o serviço de voz da Microsoft; isso pode incluir nomes de projetos, o texto a ser enviado e o pedido de permissão. Com `"tts": "piper"`, a síntese é local; se falhar, usa a voz do Windows.

## Licenças

O código próprio é [MIT](LICENSE). As dependências têm licenças próprias ([avisos de terceiros](THIRD_PARTY_NOTICES.md)):

- **piper-tts é GPL-3.0 ou posterior.** Distribuir um pacote que inclua o Piper (instalador, executável, pasta com dependências) exige cumprir a GPL.
- **edge-tts** é LGPL-3.0 (exceto um arquivo MIT) e **pystray** é LGPL-3.0.
- **Modelos pré-treinados do openWakeWord** (inclui `hey_jarvis`) são CC BY-NC-SA 4.0: **uso não comercial**. Para uso comercial, troque o modelo ou use `"wake_mode": "whisper"`, que não usa o modelo de ativação.
