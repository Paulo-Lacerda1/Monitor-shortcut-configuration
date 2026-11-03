# Monitores e Atalhos

Aplicação nativa em português para Ubuntu com ambiente GNOME. Permite organizar
monitores e guardar disposições em perfis com atalhos globais. Usa GTK 4,
libadwaita e a interface DisplayConfig do Mutter, incluindo em Wayland.

## Instalar e abrir

Dentro desta pasta:

```bash
/usr/bin/python3 install.py
```

Procura **Monitores e Atalhos** no menu de aplicações. A instalação é feita
apenas para o teu utilizador, sem sudo. As dependências já estão disponíveis
no computador em que a aplicação foi criada. Noutro Ubuntu, se necessário:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
```

Também podes abrir diretamente com `/usr/bin/python3 app.py`.
Instala antes de guardar atalhos: estes apontam para o local da aplicação.

## Usar

1. Arrasta os monitores na área de pré-visualização. As margens encaixam quando
   estão próximas. Os monitores devem tocar-se sem se sobrepor.
2. Clica num monitor ou escolhe-o na lista para mudar a resolução, frequência,
   rotação, escala, monitor principal ou estado ligado/desligado.
   Ajusta também o **Brilho** de cada monitor e clica em **Aplicar brilho agora**
   para alterar o brilho real sem guardar um perfil nem aplicar a disposição.
3. Clica em **Aplicar** para testar. Tens 20 segundos para confirmar; um processo
   separado repõe a disposição anterior se não confirmares ou se a janela falhar.
4. Clica em **Guardar perfil e atalho**, escolhe um nome e pressiona a combinação
   de teclas. Por exemplo, Ctrl+Shift+2. A aplicação verifica conflitos com outros
   atalhos personalizados e com os principais atalhos do GNOME.
5. O perfil e o atalho ficam no disco. O atalho funciona em qualquer aplicação,
   com a janela fechada e depois de terminares sessão ou reiniciares.

Para **atualizar o teu Ctrl+Shift+2 com a posição atual**:

1. Escolhe **Monitor à direita** na lista de perfis importados.
2. Clica em **Ler posição atual**.
3. Clica em **Guardar perfil e atalho** e guarda com a mesma combinação.

Os scripts existentes `~/.local/bin/monitor-N` são importados como dados,
sem serem executados nem modificados. O comando original de cada atalho só é
substituído quando guardas esse perfil na aplicação. Ao eliminar um perfil
importado, o comando original é reposto.

## O que fica guardado

- Perfis: `~/.config/monitores-atalhos/profiles.json`.
- Atalhos: configurações nativas do GNOME, no dconf.
- Aplicação instalada: `~/.local/share/monitores-atalhos/`.
- Registos temporários de recuperação: `~/.cache/monitores-atalhos/`.

Os perfis guardam posição, resolução, frequência, escala, orientação, monitor
principal, monitores ativos e o brilho de cada ecrã quando é possível lê-lo.
**Aplicar** e os atalhos repõem também o brilho guardado. A reposição automática
repõe os valores de brilho anteriores. Perfis antigos sem brilho mantêm o brilho
atual. Guardar um perfil não altera o brilho nem aplica a disposição.
Ao reiniciar, o Ubuntu inicia com a sua configuração habitual; os perfis ficam
disponíveis através dos atalhos. Esta versão não aplica um perfil automaticamente
no início de sessão.

## Brilho dos monitores

O ecrã do portátil usa o controlo de brilho do GNOME. Os monitores externos
usam a biblioteca `libddcutil` e o comando DDC/CI de brilho (VCP 0x10), que altera
o brilho do monitor. Não é aplicada uma camada escura sobre a imagem.

Neste computador, a biblioteca e as permissões de acesso já estão disponíveis.
Noutro Ubuntu 24.04, instala-a com `sudo apt install libddcutil4`. Ativa DDC/CI
no menu físico do monitor e garante que o teu utilizador tem acesso ao respetivo
`/dev/i2c-*`. A aplicação mostra o erro quando não consegue ler ou alterar o
brilho. Modos HDR, DCR ou brilho automático do monitor podem impedir o ajuste.

O controlo é separado por monitor: seleciona cada um, escolhe o valor e usa
**Aplicar brilho agora**. **Ler posição atual** atualiza também a leitura do brilho.
A demonstração apresenta os controlos sem modificar os ecrãs reais.

## Limites

- Destina-se ao GNOME/Mutter; não suporta KDE, Xfce ou outros compositores.
- Os monitores de um perfil têm de estar ligados e os respetivos modos disponíveis.
  Perfis antigos com outros monitores podem ser editados usando **Ler posição atual**.
- Não edita disposições em espelho. Separa os monitores nas Definições do Ubuntu.
- A captura de teclas ocorre apenas na janela **Gravar combinação de teclas**.
  Não existe serviço de registo de teclas em segundo plano.
- O GNOME pode pedir autorização para suspender atalhos do sistema enquanto
  gravas uma combinação. As teclas são guardadas como combinação, não como texto.
- A confirmação com reposição automática aplica-se ao botão **Aplicar**.
  Atalhos aplicam diretamente os perfis que guardaste; testa-os antes de guardar.

## Verificar e remover

```bash
/usr/bin/python3 tests.py
/usr/bin/python3 app.py --demo
/usr/bin/python3 install.py --uninstall
```

A demonstração não altera monitores nem regista atalhos reais. Ao desinstalar,
os atalhos criados pela aplicação são removidos e os comandos originais dos
atalhos importados são repostos. Os perfis são preservados no disco.

Referências de implementação: [DisplayConfig do Mutter](https://github.com/GNOME/mutter/blob/main/src/org.gnome.Mutter.DisplayConfig.xml),
[API de libddcutil](https://www.ddcutil.com/c_api/)
e [captura de teclas no GTK](https://docs.gtk.org/gtk4/signal.EventControllerKey.key-pressed.html).
