# PCPilot

PCPilot permite que um agente de IA controle o desktop GNOME com mouse e teclado durante uma sessão visível. O daemon captura a entrada física enquanto a IA trabalha; a extensão mostra uma borda fina e um aviso com o nome do agente, no visual de cada IA: Claude em coral, Codex em azul-violeta, Gemini em azul e rosa, e as outras em cinza-azulado. **Ctrl+Alt+Esc** encerra a sessão na hora e devolve o controle.

## Uso

Depois de instalar, **saia da sessão GNOME e entre novamente uma vez** para carregar a extensão. Então peça ao Claude, por exemplo: **“Abra o Firefox e pesquise por previsão do tempo em São Paulo.”** O agente inicia a sessão quando precisar interagir. A borda colorida e o aviso permanecem visíveis enquanto ele controla o computador. O Gemini também fica configurado pelo instalador para uso quando estiver instalado.

Uma sessão termina quando o agente conclui, quando você usa Ctrl+Alt+Esc, após **2 minutos sem atividade do agente** ou ao atingir **30 minutos**. Para encerrar por outro terminal ou por SSH:

```sh
sudo pcpilot release
# ou, para parar o serviço:
sudo systemctl stop pcpilot
```

## Configuração

As opções ficam em `/etc/pcpilot/config.json`. O instalador cria esse arquivo a partir de `config.example.json` somente quando ele ainda não existe. Entre as opções estão `hotkey`, `idle_timeout_s` (120), `max_session_s` (1800), `agent_uids` ([0]) e `exclude_name_patterns` ([]). Edite o JSON e reinicie o serviço para aplicar alterações:

```sh
sudo systemctl restart pcpilot
```

Os dispositivos `libvirtualhid` (controle remoto) também são bloqueados durante a sessão. Para excluí-los da captura, configure `"exclude_name_patterns": ["libvirtualhid"]`.

Para remover o PCPilot, execute `sudo ./uninstall.sh` no diretório do projeto.

## Privacidade e uso econômico

Capturas de tela são feitas apenas durante uma sessão ativa e ficam temporariamente no diretório de runtime do usuário. O PCPilot não expõe screenshots a outros programas. Prefira pedir ações agrupadas e usar elementos acessíveis e atalhos; solicite uma captura de tela somente quando necessário. Assim o agente envia menos dados visuais e usa menos tokens.

## Limitações

As posições de elementos GTK4 no Wayland são mapeadas e foram validadas com um monitor e escala de 100%. Em outros toolkits (X11, Qt, Electron), com escala fracionada, vários monitores ou pop-ups, um elemento pode aparecer com posição `?`; nesses casos, a IA usa o teclado ou uma captura de tela.

O bloqueio garantido é o do **teclado** (é dele que sai o Ctrl+Alt+Esc). Sem nenhum teclado capturável, a sessão nem começa. Um mouse ou touchscreen que outro programa já esteja capturando fica com você durante a sessão, porque o PCPilot prefere deixar um dispositivo livre a te trancar fora.

Com o monitor desligado (DPMS) a sessão não inicia, porque a borda precisa aparecer antes de qualquer controle. Os cantos ativos do GNOME ficam desligados enquanto a IA controla.
