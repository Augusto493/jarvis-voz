# Avisos de terceiros

## Coucou

O layout, as medidas, a paleta, a tipografia e as curvas de animação da ilha (`ui/island.html`, `island_ui.py`) derivam do código de [Coucou](https://github.com/Louis-CFM/coucou). O nome, o Mochi, os ícones e os sons do Coucou são reservados e não fazem parte deste projeto.

```
MIT License

Copyright (c) 2026 Louis Raillé

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Dependências Python e modelos

Não são incluídos neste repositório: são instalados pelo `pip` ou baixados na primeira execução. A licença MIT deste projeto vale só para o código próprio. Licenças conferidas nos metadados das versões instaladas indicadas (o `requirements.txt` não fixa versões).

| Componente | Licença | Observação |
|---|---|---|
| [piper-tts](https://github.com/OHF-voice/piper1-gpl) 1.8.0 | GPL-3.0-or-later | Importado pelo `jarvis.py` com `"tts": "piper"`. Distribuir um pacote que inclua o Piper exige cumprir a GPL (inclusive oferecer o código-fonte correspondente). |
| [edge-tts](https://github.com/rany2/edge-tts) 7.3.1 | LGPL-3.0 | Exceção: `edge_tts/srt_composer.py` é MIT. |
| [pystray](https://github.com/moses-palmer/pystray) 0.19.5 | LGPL-3.0 | |
| [openWakeWord](https://github.com/dscripka/openWakeWord) 0.6.0 | Código: Apache-2.0. Modelos pré-treinados: [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | Os modelos, inclusive `hey_jarvis`, são para uso **não comercial**, com atribuição e compartilhamento pela mesma licença. |

As vozes do Piper têm licenças próprias, não conferidas aqui. As demais dependências também mantêm suas licenças.

