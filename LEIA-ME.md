# AvisoSonoro

Toca 3 áudios em sequência a cada 15 minutos durante o horário configurado, abaixando o som de qualquer app (navegador, Spotify, Teams…) e devolvendo o volume original no final.

## Ciclo

| Etapa | O que acontece | Padrão |
|---|---|---|
| 1 | Fade-out de cada app que emite som | até 15% em 4 s |
| 2 | Fade-in do player do AvisoSonoro | até 50% em 2 s |
| 3 | Áudios em sequência, cada um uma única vez | 3 arquivos |
| 4 | Fade-in de cada app de volta ao volume que tinha | 3 s |

Apps que começarem a tocar *durante* o aviso também são abaixados. Se você mexer no volume de um app durante o aviso, sua escolha é mantida.

## Instalação no PC

1. Copie a pasta `AvisoSonoro` (com `AvisoSonoro.exe`, `config.json` e `audios\`) para, por exemplo, `C:\AvisoSonoro`. Evite `C:\Program Files` (pasta protegida).
2. Coloque os 3 áudios em `audios\` (ordem alfabética = ordem de reprodução) ou liste-os em `"audios"` no `config.json`.
3. Execute `AvisoSonoro.exe`. Aparece um ícone azul perto do relógio.
4. Pronto: na primeira execução ele já se configura para iniciar com o Windows (sem administrador). Para desligar isso, use o menu do ícone ou `"iniciar_com_windows": false`.

Menu do ícone: **Tocar agora** (teste) · **Pausar avisos** · **Iniciar com o Windows** · **Abrir pasta** · **Sair**.

Na primeira execução o Windows pode mostrar "O Windows protegeu o computador" (executável sem assinatura digital): clique em **Mais informações → Executar assim mesmo**.

## Configuração (`config.json`)

| Campo | Padrão | Significado |
|---|---|---|
| `horario_inicio` / `horario_fim` | 08:00 / 18:00 | Janela do dia; avisos em 08:00, 08:15 … 18:00 |
| `intervalo_minutos` | 15 | Intervalo entre avisos (contado a partir do início) |
| `dias_semana` | [0..4] | 0 = segunda … 6 = domingo |
| `volume_apps_durante_aviso` | 0.15 | Nível dos outros apps durante o aviso |
| `volume_player` | 0.50 | Volume dos áudios do aviso |
| `fade_saida_apps_seg` / `fade_entrada_player_seg` / `fade_retorno_apps_seg` | 4 / 2 / 3 | Durações dos fades |
| `tolerancia_atraso_seg` | 120 | PC acordou da suspensão atrasado? Só toca se o atraso for menor que isto |

Alterações valem a partir do próximo horário, sem reiniciar.

## Gerar o .exe

**Opção A — sem Python no PC (GitHub Actions):** suba esta pasta para um repositório GitHub → aba **Actions** → *Build AvisoSonoro.exe* → **Run workflow**. Baixe o zip em *Artifacts*.

**Opção B — local:** instale Python 3.12 (64 bits) e rode `build.bat`. Resultado em `dist\AvisoSonoro\`.

Para testar sem gerar o .exe: `pip install -r requirements.txt` e `python aviso_sonoro.py --testar`.

## Linha de comando

| Parâmetro | Função |
|---|---|
| `--testar` | Executa um ciclo agora e sai |
| `--listar-sessoes` | Registra no log os apps com áudio e seus volumes |
| `--instalar-inicializacao` / `--remover-inicializacao` | Liga/desliga início automático |
| `--sem-bandeja` | Roda sem ícone |

## Diagnóstico

O log fica em `logs\aviso_sonoro.log` (mostra quais apps foram abaixados, de quanto, e se algum áudio falhou). Se a pasta não for gravável, vai para `%LOCALAPPDATA%\AvisoSonoro\logs`.

Se o programa for encerrado à força no meio de um aviso, o volume dos apps é corrigido automaticamente na próxima abertura.

## Limitações conhecidas

- Windows 10 e 11, 64 bits. Windows 7/8 não são suportados (o Python atual não roda neles).
- Apps em modo de áudio exclusivo (alguns softwares de DJ/ASIO) não aceitam controle de volume externo.
- Formatos WMA/M4A não são lidos: converta para MP3.
- Alguns antivírus corporativos marcam executáveis PyInstaller como suspeitos; nesse caso, peça à TI para liberar o arquivo ou gere o .exe com `--onedir` em vez de `--onefile`.
