# Sıfırdan kurulum: WSL + Claude Code + OpenCode / Command Code işçileri + herdr + Heeler

Windows PC'de, WSL içinde bir Claude Code "lider" oturumu; ucuz DeepSeek modeliyle çalışan, sandbox'a kapatılmış OpenCode ve Command Code "işçileri"; her işi ayrı bir git worktree'de yürüten `lane` aracı; her şeyi terminal sekmesi olarak gösteren herdr; telefondan izlemek için Heeler. Bu rehber, bizim evde her gün kullandığımız düzenin adım adım, sıfırdan kurulumudur.

Komutlar Ubuntu 24.04 (WSL2) ve Windows 11 içindir. `<KÖŞELİ_PARANTEZ>` içindeki yerleri kendi değerlerinle değiştir. Dosya yolları önerimizdir; değiştirirsen her yerde aynı değiştir.

## 0. Büyük resim

| Parça | Ne yapar | Güven seviyesi |
|---|---|---|
| Claude Code (lider) | Planlar, işi böler, brief yazar, her diff'i okur, testleri kendisi koşar, mutant ekler, commit ve merge yapar | Yüksek; ama dışarıya dönük her adım için yine senin onayın |
| OpenCode (`oc-worker`) | Varsayılan işçi. DeepSeek V4.1 Flash ile kod ve test yazar | Düşük: sandbox içinde, her sonucu yeniden test edilir |
| Command Code (`cc-worker`) | Yedek işçi, aynı sandbox tasarımı, aynı model | Düşük: aynı kurallar |
| `lane` (agent-lanes) | Worktree açar, işçiyi başlatır, bekler, defter tutar, testleri ve mutantları koşar, merge eder | İşçi kodunu sandbox dışında çalıştırır, bu yüzden önce diff okunur |
| herdr | Her makinede terminal çalışma alanı sunucusu: konu başına workspace, her işçiye bir sekme | Soketi her pane'e yazabilir, bu yüzden asla sandbox'a girmez |
| Heeler | iPhone / iPad / Mac istemcisi: herdr'a SSH ile bağlanır, iş bitince push gönderir | Cihaz başına ayrı SSH anahtarı, şifre girişi kapalı |
| Tailscale | Bütün makineler ve telefon arasında özel ağ | İnternete açık tek port yok |
| WezTerm | Windows'ta terminal; WSL'i ve herdr'ı açar | - |

Akış kısaca: sen Claude'a bir iş söylersin → Claude işi dosya bazında 2-5 parçaya böler → her parça için `lane new` + brief → işçiler paralel çalışır, herdr'da sekme olarak görünür → biten işçi `.lane/REPORT.md` ve `.lane/LANE_RESULT.json` yazar → Claude diff'i okur, kendi testlerini ve mutantlarını koşar (`lane accept`) → geçen iş `lane merge` ile ana dala girer. Sen bunu telefondan Heeler ile izlersin.

Kurulum sırası (bu sırayla git, her adım bir öncekine dayanıyor):

1. WSL2 + Ubuntu 24.04 + systemd
2. Temel paketler, Node.js, PATH
3. WezTerm
4. Tailscale
5. herdr (+ kendi kendine başlayan servis)
6. Claude Code + CLAUDE.md düzeni
7. OpenCode işçisi (`oc-worker`)
8. Command Code işçisi (`cc-worker`)
9. agent-lanes (`lane`)
10. Telefon: Windows sshd + WSL sshd + Heeler
11. (İsteğe bağlı) ikinci makine
12. Yeniden başlatma sonrası rutin
13. Güncelleme rutini
14. Güvenlik kuralları
15. Sorun giderme
16. Kopya kağıdı
17. Claude talimat dosyaları (CLAUDE.md şablonları)

## 1. WSL2 + Ubuntu 24.04 + systemd

Windows'ta **yönetici** PowerShell aç:

```powershell
wsl --install -d Ubuntu-24.04
```

Bilgisayarı yeniden başlat. Açılışta Ubuntu penceresi kullanıcı adı ve şifre ister: kısa, küçük harfli bir ad seç (bu rehberde WSL kullanıcın `$USER`). Şifreyi unutma, `sudo` için lazım.

Sürümü kontrol et (WSL 2.x olmalı, dağıtım VERSION 2):

```powershell
wsl --version
wsl -l -v
```

**Ağ: mirrored mod.** Windows'ta `%USERPROFILE%\.wslconfig` dosyasını oluştur (yoksa). Bu mod sayesinde Windows'un `127.0.0.1`'i WSL'e de gider; telefondan gelen SSH, Windows'tan WSL'e doğrudan atlayabilir.

```ini
[wsl2]
networkingMode=mirrored
# İsteğe bağlı: WSL'e verilecek RAM üst sınırı
# memory=24GB
```

**systemd.** Ubuntu içinde `/etc/wsl.conf`:

```bash
sudo tee /etc/wsl.conf >/dev/null <<EOF
[boot]
systemd=true

[user]
default=$USER
EOF
```

Sonra Windows'ta `wsl --shutdown`, Ubuntu'yu tekrar aç ve kontrol et:

```bash
systemctl --user status        # "State: running" görmelisin
ps -p 1 -o comm=               # systemd yazmalı
```

**Dosyalar nerede dursun:** projeleri WSL'in kendi diskinde tut (`~/...`), `/mnt/c/...` altında değil. `/mnt/c` üzerinden git ve npm çok yavaş, dosya izinleri de bozuk görünür. Bu rehberdeki düzen:

```text
~/ai/projects/<konu>/          # her konu (proje) ayrı klasör, her birinin kendi CLAUDE.md'si
~/agent-lanes/repo             # lane'lerin dallandığı ana repo (ya da LANE_MAIN_REPO ile başka bir repo)
~/agent-lanes/worktrees/       # işçi worktree'leri: oc-worker-01, cc-worker-03 ...
~/agent-lanes/lanes/           # lane defteri, loglar, ACCEPT_REPORT.md (işçiler buraya ulaşamaz)
~/.local/bin, ~/.local/lib     # kullanıcı seviyesinde programlar (sudo gerektirmez)
```

```bash
mkdir -p ~/ai/projects ~/agent-lanes/worktrees ~/agent-lanes/lanes ~/.local/bin ~/.local/lib
```

## 2. Temel paketler, Node.js, PATH

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential git curl wget unzip xz-utils jq ripgrep rsync \
  python3 python3-venv python3-pip bubblewrap openssh-client
bwrap --version                # bubblewrap 0.9 veya üstü
```

`bubblewrap` (bwrap) işçi sandbox'ının temeli: işçinin göreceği dosya sistemini sıfırdan, sadece izin verdiğimiz klasörlerle kurar.

**Node.js (kullanıcı seviyesinde, sudo'suz).** OpenCode, Command Code ve Heeler eklentisi Node 22+ ister. Node'u `~/.local/lib/nodejs` altına koyuyoruz; böylece işçi sandbox'ı onu salt okunur görebilir (nvm `~/.nvm` altına kurar, sandbox onu görmez).

```bash
cd /tmp
B=https://nodejs.org/dist/latest-v26.x
curl -fsSLO $B/SHASUMS256.txt
F=$(grep -o 'node-v[0-9.]*-linux-x64\.tar\.xz' SHASUMS256.txt | head -1)
curl -fsSLO $B/$F && grep " $F\$" SHASUMS256.txt | sha256sum -c -     # "OK" yazmalı
mkdir -p ~/.local/lib/nodejs && tar -xJf $F -C ~/.local/lib/nodejs
ln -sfn ${F%.tar.xz} ~/.local/lib/nodejs/current        # göreli bağ: sandbox içinde de çözülür
```

**PATH.** `~/.bashrc` dosyasının **en üstüne** (Ubuntu'nun "If not running interactively, don't do anything" satırından **önce**) ekle. Önce olması önemli: SSH ile etkileşimsiz komut çalıştırıldığında (`ssh pc 'lane ls'`, herdr uzak makine) `.bashrc` o satırda durur ve PATH'in altındaki kısmı hiç okunmaz.

```bash
# --- ~/.bashrc en üstü ---
export PATH="$HOME/.local/bin:$HOME/.local/lib/nodejs/current/bin:$PATH"
```

```bash
source ~/.bashrc
npm config set prefix ~/.local        # npm i -g artık ~/.local altına kurar, sudo gerekmez
node --version && npm --version && command -v node
```

**Git kimliği** (commit'leri Claude atacak, ama senin adınla):

```bash
git config --global user.name "<ADIN>"
git config --global user.email "<EPOSTAN>"
git config --global init.defaultBranch main
```

## 3. WezTerm (Windows terminali)

Windows PowerShell:

```powershell
winget install wez.wezterm
```

`%USERPROFILE%\.wezterm.lua` (en küçük çalışan hali; Alt+L ile menüden WSL açılır):

```lua
local wezterm = require("wezterm")
local act = wezterm.action
local config = wezterm.config_builder()

-- Varsayılan: doğrudan WSL Ubuntu, ev klasöründe
config.default_prog = { "wsl.exe", "-d", "Ubuntu-24.04", "--cd", "~" }

config.launch_menu = {
  { label = "WSL Ubuntu-24.04", args = { "wsl.exe", "-d", "Ubuntu-24.04", "--cd", "~" } },
  { label = "WSL + herdr", args = { "wsl.exe", "-d", "Ubuntu-24.04", "--cd", "~", "--", "bash", "-lc", "herdr" } },
  { label = "PowerShell", args = { "powershell.exe" } },
}

config.keys = {
  { key = "l", mods = "ALT", action = act.ShowLauncher },
  { key = "c", mods = "CTRL|SHIFT", action = act.CopyTo("Clipboard") },
  { key = "v", mods = "CTRL|SHIFT", action = act.PasteFrom("Clipboard") },
  { key = "k", mods = "CTRL|SHIFT", action = act.ClearScrollback("ScrollbackAndViewport") },
}

config.scrollback_lines = 20000
config.max_fps = 120
return config
```

Not: herdr'ın kendi kısayol öneki `Ctrl+b`. WezTerm'de `Ctrl+b`'yi başka bir şeye bağlama.

## 4. Tailscale (özel ağ)

1. Windows: `winget install tailscale.tailscale`, sonra Tailscale'e giriş yap.
2. Telefon / iPad: App Store'dan Tailscale, aynı hesapla giriş.
3. Tailscale yönetim panelinde makine adlarını kısa ve anlamlı yap (ör. `pc`, `laptop`), MagicDNS açık kalsın. Bu rehberde PC'nin tailnet adı `<PC_TAILNET_ADI>`.
4. WSL mirrored modda olduğu için tailnet WSL içinden de görünür: `ping <PC_TAILNET_ADI>` WSL içinden de çalışmalı.

İnternete hiçbir port açmıyoruz. SSH sadece tailnet üzerinden (adım 10).

İsteğe bağlı: Tailscale ACL'lerinde sadece kendi cihazlarının birbirine SSH (22) açabildiğinden emin ol. Heeler'ın kendi uygulama içi Tailscale'ini açarsan o ayrı bir cihaz olarak eklenir; ACL'de ona da izin gerekir. Telefonda zaten Tailscale uygulaması varsa Heeler'ınkini kapalı bırakmak daha basit.

## 5. herdr

herdr: her makinede çalışan terminal çalışma alanı sunucusu. Workspace (konu) → sekme → pane. Terminal kapansa da içindekiler çalışmaya devam eder; WezTerm, Heeler veya başka bir makine aynı oturuma bağlanır.

```bash
curl -fsSL https://herdr.dev/install.sh | sh
command -v herdr && herdr --version
herdr                    # ilk açılış: varsayılan oturumu başlatır ve bağlanır
```

Temel tuşlar (önek `Ctrl+b`: bas, bırak, sonra tuş):

| Tuş | İş |
|---|---|
| `Ctrl+b` `?` | Tüm kısayollar |
| `Ctrl+b` `c` | Yeni sekme |
| `Ctrl+b` `v` | Sağa böl |
| `Ctrl+b` `-` | Aşağı böl |
| `Ctrl+b` `q` | Ayrıl (detach): pane'ler sunucuda çalışmaya devam eder; `herdr` ile geri dön |

Fare her şey için çalışır. Ayarlar: `~/.config/herdr/config.toml` (varsayılanları görmek için `herdr --default-config`, değişikliği uygulamak için `herdr server reload-config`).

**Ajan entegrasyonları.** herdr, Claude Code ve OpenCode'un "çalışıyor / bekliyor / bitti" durumunu sekmede gösterebilir:

```bash
herdr integration install claude      # ~/.claude/hooks altına küçük bir hook koyar
herdr integration install opencode    # ~/.config/opencode/plugins altına
herdr integration status
```

Bunlar senin **etkileşimli** Claude / OpenCode oturumların için. İşçi sandbox'ı bu yapılandırmayı zaten görmez (sandbox'ta `XDG_CONFIG_HOME` boş bir geçici klasör); herdr soketi asla sandbox'a girmez (adım 14).

**Konu başına workspace.** Her proje için bir workspace aç ve adını ver (sol kenar çubuğundan ya da `herdr workspace --help`). Biz: ana proje, `lanes` (işçi sekmeleri), sistem işleri, yan projeler. Her workspace'in ilk sekmesinde ilgili klasöre `cd` edip `claude` başlatırız.

**herdr'ı otomatik başlatan systemd servisi.** WSL açılınca herdr sunucusu kendiliğinden kalksın. Sorun: systemd servisleri Windows PATH'ini almaz, o zaman herdr pane'lerinde `powershell.exe`, `cmd.exe`, `explorer.exe` çalışmaz. Bu küçük betik Windows PATH'ini sorup ekliyor:

```bash file=~/.local/bin/herdr-server-start
#!/bin/bash -l
# herdr-server systemd kullanıcı servisi bunu çalıştırır.
# systemd servisleri, wsl.exe'nin normal kabuklara eklediği Windows PATH'ini almaz; o zaman pane'lerde
# cmd.exe / powershell.exe olmaz. Windows'a PATH'ini sor, WSL yollarına çevir, ekle, sonra herdr'ı başlat.
win_path=""
if raw=$(cd /mnt/c 2>/dev/null && timeout 20 /mnt/c/WINDOWS/system32/cmd.exe /c 'echo %PATH%' 2>/dev/null | tr -d '\r'); then
  IFS=';' read -ra parts <<< "$raw"
  for p in "${parts[@]}"; do
    [ -n "$p" ] || continue
    u=$(wslpath -u "$p" 2>/dev/null) || continue
    u=${u%/}
    [ -d "$u" ] && win_path="$win_path:$u"
  done
fi
# yedek: standart Windows klasörleri
[ -n "$win_path" ] || win_path=":/mnt/c/WINDOWS/system32:/mnt/c/WINDOWS:/mnt/c/WINDOWS/System32/Wbem:/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0:/mnt/c/WINDOWS/System32/OpenSSH"
export PATH="$PATH$win_path"
exec herdr server
```

```ini file=~/.config/systemd/user/herdr-server.service
[Unit]
Description=herdr server (kalıcı herdr oturumu; WezTerm, Heeler ve uzak istemciler buna bağlanır)

[Service]
Type=simple
# pane'lerde Windows birlikte çalışması (powershell.exe vb.); 1_interop oturumlardan uzun yaşayan init soketi
Environment=WSL_INTEROP=/run/WSL/1_interop
Environment=SHELL=/bin/bash
# elle başlatılmış bir sunucu (terminalde `herdr`) varsa: hata verip döngüye girme, atla
ExecCondition=/bin/bash -lc '! herdr status 2>/dev/null | grep -q "status: running"'
# login kabuğu + Windows PATH: pane'ler node, oc-worker, lane, cmd.exe, powershell.exe görür
ExecStart=/bin/bash -l %h/.local/bin/herdr-server-start
# çökerse yeniden başlar; bilerek `herdr server stop` derseniz 0 ile çıkar ve kapalı kalır
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
chmod +x ~/.local/bin/herdr-server-start
herdr server stop                       # elle açtığın sunucuyu kapat (içindeki pane'ler de kapanır)
systemctl --user daemon-reload
systemctl --user enable --now herdr-server
systemctl --user status herdr-server    # active (running)
herdr                                   # artık servise bağlanır
```

Bir herdr pane'inde `powershell.exe -c 'echo ok'` çalışıyorsa PATH işi tamam.

**Linger.** Varsayılan olarak kullanıcı servisleri sen WSL'e bir kez giriş yapınca başlar (WezTerm'de WSL açınca). WSL'in kendisi başka bir şey yüzünden açıldığında da kalksınlar istersen:

```bash
sudo loginctl enable-linger $USER
```

Linger açık olsa bile WSL'in kendisi bir şey tarafından başlatılmalı; Windows açılışında WSL otomatik açılmaz. En kolayı: açılışta WezTerm'i başlat (Windows Başlangıç klasörüne kısayol) ya da Görev Zamanlayıcı'da oturum açılışında `wsl.exe -d Ubuntu-24.04 --exec /bin/true`.

## 6. Claude Code + CLAUDE.md düzeni

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
cd ~/ai/projects && claude          # ilk açılışta tarayıcıdan hesabınla giriş
```

Claude Code'da bu düzen için en önemli üç şey: **talimat dosyaları**, **hafıza** ve **izinler**.

**Talimat dosyaları (CLAUDE.md).** Claude her oturum başında bunları okur:

| Dosya | Kapsam | İçine ne yazılır |
|---|---|---|
| `~/.claude/CLAUDE.md` | Her oturum, her klasör | Kim olduğun, genel çalışma kuralların, lane düzeni, asla yapılmayacaklar |
| `~/.claude/rules/*.md` | Her oturum | Makine bilgileri (`hosts.md`): Tailscale adları, ssh takma adları, hangi anahtar nerede |
| `~/ai/projects/<konu>/CLAUDE.md` | O klasörde başlatılan oturumlar | Projenin amacı, test komutu, dokunulmayacak yerler |
| `~/ai/projects/<konu>/AGENTS.md` | Diğer ajanlar (OpenCode vb.) | `CLAUDE.md`'ye symlink: `ln -s CLAUDE.md AGENTS.md` |

Hazır şablonlar en sonda (bölüm 17) ve pakette ayrı dosya olarak (`CLAUDE.md`).

Öğrendiğimiz iki tuzak:

- `@dosya` ile import, **proje klasörünün dışına** çıkan bir yolu (symlink olsa bile) sessizce yüklemiyor. Her oturumda olması gereken ortak bilgi için `~/.claude/rules/` klasörünü kullan: oradaki her `.md` her oturumda yükleniyor. Diğer ajanlar okusun istersen ortak bir yere bunun symlink'ini koy.
- Claude Code hafızası **çalışma klasörüne göre** tutuluyor (`~/.claude/projects/<klasör-adı>/memory/`). Başka bir klasörde açılan oturum o hafızayı görmez. Bu yüzden her konu kendi klasöründe, kendi herdr workspace'inde, kendi Claude oturumunda.

**Hafıza.** Claude kalıcı bilgileri `~/.claude/projects/<klasör>/memory/` altına tek-bilgi-tek-dosya yazar; `MEMORY.md` dizindir. "Bunu hatırla" dediğinde oraya yazar. Lane durumunu hafızaya yazdırma; lane defteri zaten tutuyor.

**İzinler ve auto mode.** Başlangıçta Claude her komutta onay ister. Güven oluştukça `/permissions` ile sık kullanılan, zararsız komutlara izin ver. Auto mode (Shift+Tab ile mod değişir) komutları bir sınıflandırıcıyla kendisi onaylar; bunu güvenli kullanmak için `~/.claude/settings.json` içinde ortamını ve kesin yasakları tarif et:

```json
{
  "model": "opus",
  "autoMode": {
    "environment": [
      "**Kurulum**: tek kişinin makineleri, özel bir Tailscale ağında: PC (bu WSL), <DİĞER_MAKİNELER>. herdr hepsinde çalışıyor; Heeler (iOS/Mac) ssh ile bağlanıyor",
      "**Konular** ~/ai/projects/<konu> altında; her konunun CLAUDE.md'si o konu için geçerli. Ortak makine bilgileri ~/.claude/rules/hosts.md",
      "**İşçiler**: oc-worker / cc-worker sadece `lane` üzerinden, sandbox içinde; worktree'ler ~/agent-lanes/worktrees",
      "**Asla**: herdr soketini işçi sandbox'ına koymak; anahtar, token, host key yayınlamak; git push'u sormadan yapmak"
    ],
    "soft_deny": [
      "$defaults",
      "Bash(git push:*) — sahibine sormadan uzak repoya gönderme yok"
    ]
  }
}
```

`$defaults` satırı yerleşik güvenlik kurallarını korur; sil­me. JSON geçerli olmalı: düzenledikten sonra `python3 -m json.tool ~/.claude/settings.json >/dev/null && echo ok`.

**Skill: lane.** Lane iş akışını her seferinde anlatmamak için Claude'a bir skill ver: `~/.claude/skills/lane/SKILL.md` (bölüm 17'de hazır metin). Claude "işçilere ver", "lane", "opencode ile yap" dendiğinde bunu kendisi yükler; sen de `/lane` yazarak çağırabilirsin.

**Günlük kullanım ipuçları.**

- Uzun işlerde Claude'a "bitince bana haber ver" deme; işçiyi arka planda başlatır, bitince sistem onu uyandırır.
- Büyük bir değişiklikten önce `Shift+Tab` ile plan moduna al: önce plan, onayından sonra kod.
- `claude -c` son konuşmaya, `claude -r` geçmiş bir oturuma döner. Bağlam dolunca Claude kendisi özetler; kritik kararları hafızaya ya da proje dosyasına yazdır.
- Claude'a bir işi yaptırınca "test çıktısını göster" de; "testler geçti" cümlesine değil çıktıya bak.

## 7. OpenCode işçisi (`oc-worker`)

**Hesap.** opencode.ai üzerinde **OpenCode Go** aboneliği aldık. Model: `opencode-go/deepseek-v4.1-flash` (ucuz, yeterince iyi; çünkü her sonucu yeniden test ediyoruz). Konsolda bölgeyi **Global** yapmak gerekti, yoksa DeepSeek modelleri gelmedi. Ücretsiz (`-free`) modelleri repo verisiyle kullanma: eğitim verisi olarak kullanılabiliyorlar. Kullandıkça öde (`opencode/...`) sağlayıcısı Go aboneliğiyle "Insufficient balance" verir; doğru önek `opencode-go/`.

**Kurulum:**

```bash
npm i -g opencode-ai
ls ~/.local/lib/node_modules/opencode-ai/bin/      # opencode.exe (adı .exe ama Linux ikilisi)
opencode --version
```

**Anahtar dosyası** (repo dışında, sadece sen okuyabilirsin):

```bash
mkdir -p ~/.config/opencode-worker && chmod 700 ~/.config/opencode-worker
nano ~/.config/opencode-worker/api-key      # anahtarı tek satır yapıştır, kaydet
chmod 600 ~/.config/opencode-worker/api-key
```

**Tehdit modeli.** İşçi üçüncü taraf bir modele repo içeriği gönderiyor; makinede de anahtarlar, SSH anahtarları, Claude girişi var. O yüzden:

- Sandbox'ta sadece işletim sistemi (`/usr`, `/etc`), program dosyaları (`~/.local/lib`, salt okunur) ve **sadece o işin repo klasörü** var. `~/.ssh`, `~/.claude`, `~/.config`, diğer projeler, `/mnt/c` hiç yok.
- `--write` sadece işçinin kendi worktree'sinde (`oc-worker-NN`). `.git` salt okunur ve asıl repo görünmez: işçi git kullanamaz, commit atamaz.
- API anahtarı ortam değişkeni ya da argüman olarak değil, **dosya tanımlayıcısı 3** ile girer, bir kez okunur, kapatılır. Bir eklenti, ajanın açtığı her kabukta anahtarı boşaltır. Her ajan kabuğu ayrıca iç içe bir kullanıcı+pid namespace'inde çalışır: OpenCode sürecini göremez, belleğini/ortamını okuyamaz.
- Her çalıştırma kendi veri klasörünü alır (paralel çalıştırmalar tek SQLite'ı paylaşınca "database is locked" hatası veriyor).
- Kapsanmayan: ağ açık (model API'si için şart). İşçi repo içeriğini dışarı gönderebilir; bu yüzden repoya sadece bir üçüncü tarafa göstereceğin şeyi koy, gizli bilgiyi repo dışında tut.

**Dosyalar.** Paketteki `scripts/` klasöründe hazır hepsi var (bölüm 16'da tek komutla kurulum). Tek tek:

```bash file=~/.local/bin/oc-worker
#!/usr/bin/env bash
# oc-worker: OpenCode (OpenCode Go, DeepSeek) işçisi. Mevcut repoda tek bir görev, allowlist sandbox içinde.
#   oc-worker [-m MODEL] "soru"             salt okunur: düzenleme yok, kabuk yok
#   oc-worker --write [-m MODEL] "görev"    düzenler ve komut çalıştırır; sadece kendi worktree'sinde (oc-worker-NN)
# Kimlik bilgileri, diğer klasörler ve Windows sürücüleri sandbox'ta yok. Anahtar 3 numaralı tanımlayıcıyla girer
# (bkz. ~/.local/lib/oc-worker/key-env.sh) ve bir eklenti ajanın açtığı her kabukta onu boşaltır.
set -euo pipefail
KEY="$HOME/.config/opencode-worker/api-key"; L="$HOME/.local/lib/oc-worker"
WT=$(readlink -m "${LANE_WORKTREES:-$HOME/agent-lanes/worktrees}")
PROJ=$(readlink -m "${AI_PROJECTS:-$HOME/ai/projects}")
OC_BIN="$HOME/.local/lib/node_modules/opencode-ai/bin/opencode.exe"
MODEL=opencode-go/deepseek-v4.1-flash; MODE=ro
if [ "${1:-}" = "--write" ]; then MODE=rw; shift; fi
if [ "${1:-}" = "-m" ]; then [ $# -ge 2 ] || { echo "oc-worker: -m bir model ister" >&2; exit 2; }; MODEL=$2; shift 2; fi
[ $# -eq 1 ] && [ -n "$1" ] && [ "${1#-}" = "$1" ] || { echo 'kullanım: oc-worker [--write] [-m MODEL] "görev"' >&2; exit 2; }
HERE=$(pwd -P); ROOT=$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || echo "$HERE")
case "$ROOT/" in
  "$WT"/?*|"$PROJ"/?*|/tmp/?*) ;;
  *) echo "oc-worker: reddedildi, izinli bir klasör değil: $ROOT" >&2; exit 3;;
esac
if [ "$MODE" = rw ] && ! [[ "$ROOT" =~ ^"$WT"/oc-worker(-[0-9]{1,3})?$ ]]; then
  echo "oc-worker: --write sadece $WT/oc-worker veya oc-worker-NN içinde çalışır" >&2; exit 3
fi
[ -f "$KEY" ] && [ "$(stat -c %a "$KEY")" = 600 ] || { echo "oc-worker: anahtar dosyası yok ya da izni 600 değil: $KEY" >&2; exit 4; }
[ -x "$OC_BIN" ] || { echo "oc-worker: opencode bulunamadı: $OC_BIN (npm i -g opencode-ai)" >&2; exit 5; }
ARGS=(run -m "$MODEL" --dir "$HERE")
if [ "$MODE" = rw ]; then ARGS+=(--auto); fi
exec 3<"$KEY" </dev/null   # görev argümanla gelir; açık kalmış bir stdin'i beklemesin (opencode run onu okur)
exec "$L/sandbox.sh" "$MODE" "$ROOT" "$HERE" "$L/key-env.sh" "$OC_BIN" "${ARGS[@]}" "$1"
```

```bash file=~/.local/lib/oc-worker/sandbox.sh
#!/usr/bin/env bash
# OpenCode için allowlist sandbox: sandbox.sh ro|rw ROOT WORKDIR komut...
# İçeride sadece işletim sistemi, program dosyaları (salt okunur), işçi durumu ve ROOT var.
# rw: ROOT yazılabilir ama .git salt okunur. Proje içindeki opencode yapılandırması yok sayılır.
set -euo pipefail
MODE=$1; ROOT=$2; WORK=$3; shift 3
H=$HOME; S=$H/.local/state/oc-worker
RUN=$(date -u +%Y%m%dT%H%M%S)-$$; mkdir -p "$S/runs/$RUN" "$S/cache"   # her çalıştırmaya ayrı sqlite
if [ "$MODE" = rw ]; then
  REPO=(--bind "$ROOT" "$ROOT")
  if [ -e "$ROOT/.git" ]; then REPO+=(--ro-bind "$ROOT/.git" "$ROOT/.git"); fi
  PERM='{"edit":"allow","bash":"allow","webfetch":"deny","websearch":"deny","external_directory":"allow"}'
else
  REPO=(--ro-bind "$ROOT" "$ROOT")
  PERM='{"edit":"deny","bash":"deny","webfetch":"deny","websearch":"deny","external_directory":"deny"}'
fi
# WSL'de /etc/resolv.conf, /mnt/wsl/resolv.conf'a symlink: o tek dosya bağlanmazsa DNS çalışmaz
DNS=(); if [ -e /mnt/wsl/resolv.conf ]; then DNS=(--ro-bind /mnt/wsl/resolv.conf /mnt/wsl/resolv.conf); fi
exec bwrap --die-with-parent --new-session --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try --clearenv \
  --setenv HOME "$H" --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv LANG C.UTF-8 --setenv TERM dumb --setenv NO_COLOR 1 \
  --setenv XDG_CONFIG_HOME /tmp/oc-cfg --setenv XDG_DATA_HOME "$S/runs/$RUN/data" --setenv XDG_STATE_HOME "$S/runs/$RUN/state" --setenv XDG_CACHE_HOME "$S/cache" \
  --setenv OPENCODE_DISABLE_PROJECT_CONFIG 1 --setenv OPENCODE_DISABLE_AUTOUPDATE 1 --setenv OPENCODE_DISABLE_CLAUDE_CODE 1 \
  --setenv OPENCODE_DISABLE_EXTERNAL_SKILLS 1 --setenv OPENCODE_DISABLE_LSP_DOWNLOAD 1 --setenv OPENCODE_PERMISSION "$PERM" \
  "${DNS[@]}" --ro-bind /usr /usr --ro-bind /etc /etc --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /run --dir "$H" \
  --ro-bind "$H/.local/lib" "$H/.local/lib" --bind "$S" "$S" \
  "${REPO[@]}" --chdir "$WORK" -- "$@"
```

```bash file=~/.local/lib/oc-worker/key-env.sh
#!/bin/bash
# Sandbox içinde: anahtarı 3 numaralı tanımlayıcıdan oku, kapat, sadece opencode'a ver. Anahtar dosyası burada yok.
# OpenCode yapılandırma klasörüne yazıyor: her çalıştırma salt okunur şablonun sandbox tmpfs'teki taze bir kopyasını alır.
IFS= read -r K <&3 || true; exec 3<&-
[ -n "$K" ] || { echo "oc-worker: anahtar boş" >&2; exit 4; }
cp -r "$HOME/.local/lib/oc-worker/config" /tmp/oc-config; export OPENCODE_CONFIG_DIR=/tmp/oc-config
export SHELL="$HOME/.local/lib/oc-worker/jail-shell"   # ajan kabukları iç içe kullanıcı+pid namespace'inde
export OPENCODE_API_KEY="$K"; unset K
exec "$@"
```

```bash file=~/.local/lib/oc-worker/jail-shell
#!/bin/bash
# OpenCode'un ajan komutları için kabuğu: iç içe kullanıcı + pid namespace. İçeriden, anahtarı tutan opencode süreci
# görünmez; çekirdek, namespace sınırı boyunca ptrace / environ / mem erişimini reddeder.
B=$(command -v bwrap) || { echo "jail-shell: bwrap yok" >&2; exit 1; }
exec "$B" --unshare-user --unshare-pid --die-with-parent --dev-bind / / --proc /proc --unsetenv OPENCODE_API_KEY -- /bin/bash "$@"
```

```json file=~/.local/lib/oc-worker/config/opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "opencode-go/deepseek-v4.1-flash",
  "autoupdate": false,
  "share": "disabled",
  "enabled_providers": ["opencode-go"],
  "plugin": []
}
```

```js file=~/.local/lib/oc-worker/config/plugins/strip-key.js
// Ajanın çalıştırdığı kabuk komutları API anahtarını miras almamalı: her kabuk ortamında boşalt.
export const StripKey = async () => ({
  "shell.env": async (_input, output) => {
    if (output && output.env) { output.env.OPENCODE_API_KEY = ""; delete output.env.OPENCODE_API_KEY; output.env.OPENCODE_API_KEY = ""; }
  },
});
```

**Kendi kendine test (`check.sh`).** Model çağırmadan, düz kabuk komutlarıyla sınırı kanıtlar. Wrapper'da **her** değişiklikten sonra tekrar çalıştır; hepsi PASS olmalı.

```bash file=~/.local/lib/oc-worker/check.sh
#!/usr/bin/env bash
# oc-worker sınırını model çağırmadan kanıtlar. Sadece ad ve PASS/FAIL yazar; hiçbir gizli bilgiyi yazdırmaz.
# Bir test worktree'si ister: varsayılan $LANE_WORKTREES/oc-worker-01 (ya da CHECK_W=/yol).
set -u
L=$HOME/.local/lib/oc-worker; WT=${LANE_WORKTREES:-$HOME/agent-lanes/worktrees}; W=${CHECK_W:-$WT/oc-worker-01}
KEY=$HOME/.config/opencode-worker/api-key; pass=0; fail=0
ok(){ if [ "$2" = yes ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
[ -d "$W" ] || { echo "test worktree'si yok: $W  (lane new oc-worker-01 --from main, ya da CHECK_W=...)" >&2; exit 2; }
sb(){ local m=$1 c=$2; shift 2; "$L/sandbox.sh" "$m" "$W" "$W" /bin/bash -c "$c" _ "$@" 2>/dev/null; }
for p in "$HOME/.ssh" "$HOME/.claude" "$HOME/.config" "$HOME/.bashrc" "$HOME/ai" /mnt/c "$(dirname "$W")/../repo"; do
  [ "$(sb ro '[ -e "$1" ] && echo yes || echo no' "$p")" = no ] && ok "gizli: $p" yes || ok "gizli: $p" no
done
[ -n "$(sb ro 'ls -A | head -1')" ] && ok "kontrol: repo içeriden okunuyor" yes || ok "kontrol: repo içeriden okunuyor" no
[ -z "$(sb ro 'touch .oc-probe && echo wrote')" ] && [ ! -e "$W/.oc-probe" ] && ok "ro: repoya yazma engelli" yes || ok "ro: repoya yazma engelli" no
[ "$(sb rw 'touch .oc-probe && rm .oc-probe && echo yes')" = yes ] && ok "rw: kendi worktree'si yazılabilir" yes || ok "rw: kendi worktree'si yazılabilir" no
[ -z "$(sb rw 'echo x >> .git && echo wrote')" ] && ok "rw: .git salt okunur" yes || ok "rw: .git salt okunur" no
[ -z "$(sb rw 'git status >/dev/null 2>&1 && echo works')" ] && ok "rw: git içeride çalışmıyor" yes || ok "rw: git içeride çalışmıyor" no
n=$(sb ro 'ls /proc | grep -c "^[0-9]"'); [ "${n:-99}" -le 4 ] && ok "host süreçleri görünmüyor ($n)" yes || ok "host süreçleri görünmüyor ($n)" no
[ "$(sb ro 'touch /tmp/x && echo yes')" = yes ] && ok "kontrol: sandbox /tmp yazılabilir" yes || ok "kontrol: sandbox /tmp yazılabilir" no
[ "$(sb ro 'getent hosts opencode.ai >/dev/null && echo yes')" = yes ] && ok "kontrol: DNS çalışıyor" yes || ok "kontrol: DNS çalışıyor" no
if [ -f "$KEY" ]; then
  [ "$(stat -c %a "$KEY")" = 600 ] && ok "anahtar dosyası 600" yes || ok "anahtar dosyası 600" no
  exec 3<"$KEY"
  out=$("$L/sandbox.sh" ro "$W" "$W" "$L/key-env.sh" /bin/bash -c '
    a=missing; [ -n "${OPENCODE_API_KEY:-}" ] && a=loaded
    b=file-visible; [ -e "$HOME/.config/opencode-worker/api-key" ] || b=file-hidden
    c=fd-open; [ -e /proc/self/fd/3 ] || c=fd-closed
    d=$("$SHELL" -c "echo \${OPENCODE_API_KEY:-none}")
    e=$(OPENCODE_API_KEY= "$SHELL" -c "cat /proc/[0-9]*/environ 2>/dev/null | tr \"\\0\" \"\\n\" | grep -c \"^OPENCODE_API_KEY=.\"")   # eklentinin yaptığı gibi boş anahtarla: opencode süreci görünmemeli
    echo "$a $b $c shell-$([ "$d" = none ] && echo clean || echo LEAK) procs-$([ "$e" = 0 ] && echo clean || echo LEAK)"' 2>/dev/null)
  exec 3<&-
  [ "$out" = "loaded file-hidden fd-closed shell-clean procs-clean" ] && ok "anahtar: opencode'da var; dosya yok, fd kapalı, ajan kabuğunda yok" yes || ok "anahtar: [$out]" no
else
  ok "anahtar dosyası var ($KEY)" no
fi
echo "$pass/$((pass+fail))"; [ "$fail" -eq 0 ]
```

```bash
chmod +x ~/.local/bin/oc-worker ~/.local/lib/oc-worker/*.sh ~/.local/lib/oc-worker/jail-shell
```

Test worktree'si bölüm 9'da `lane new` ile açılınca: `~/.local/lib/oc-worker/check.sh` → `17/17`.

İlk deneme (salt okunur, ucuz):

```bash
cd ~/ai/projects/<konu> && oc-worker "Bu repoda hangi dosyalar var? 5 satırda özetle."
```

## 8. Command Code işçisi (`cc-worker`)

**Hesap.** commandcode.ai üzerinde küçük bir plan; model `deepseek/deepseek-v4.1-flash`. Mevcut model adlarını görmek için: `cmd --list-models`. (GLM 5.3 Flash ile A/B yaptık: zor işte ~3 kat hızlıydı, kalite eşit; yine de DeepSeek'te kaldık. İkisi de olur.)

```bash
npm i -g command-code
cmd --version                # komut adı "cmd" (Windows'un cmd.exe'siyle karışmaz, WSL'de)
mkdir -p ~/.config/command-code-worker ~/.commandcode && chmod 700 ~/.config/command-code-worker
nano ~/.config/command-code-worker/api-key && chmod 600 ~/.config/command-code-worker/api-key
```

Etkileşimli kullanım da mümkün: bir klasörde `cmd` (yeni oturum), `cmd -c` (son konuşmaya devam), `cmd -r` (geçmişten seç). Ama işçi olarak her zaman aşağıdaki wrapper'la.

Aynı tehdit modeli; anahtar yine 3 numaralı tanımlayıcıyla girer. Burada bir Node ön-yükleyici (`key-fd.cjs`) anahtarı süreç içinde tutar ve **her alt süreçten** siler; anahtar `/proc/self/environ`'da hiç görünmez. Yazma modunda işçinin kendi izin dosyası salt okunur bağlanır: kendi kurallarını değiştiremez.

```bash file=~/.local/bin/cc-worker
#!/usr/bin/env bash
# cc-worker: Command Code (DeepSeek) işçisi. Mevcut repoda tek bir görev, allowlist sandbox içinde.
#   cc-worker [-m MODEL] "soru"            salt okunur
#   cc-worker --write [-m MODEL] "görev"   düzenler ve komut çalıştırır; sadece kendi worktree'sinde (cc-worker-NN)
# Sınırı CLI değil çekirdek koyar: kimlik bilgileri ve diğer klasörler sandbox'ta yok. Anahtar tanımlayıcı ile girer.
set -euo pipefail
KEY="$HOME/.config/command-code-worker/api-key"; LIB="$HOME/.local/lib"
WT=$(readlink -m "${LANE_WORKTREES:-$HOME/agent-lanes/worktrees}")
PROJ=$(readlink -m "${AI_PROJECTS:-$HOME/ai/projects}")
CC_JS="$LIB/node_modules/command-code/dist/index.mjs"
NODE=$(readlink -f "$(command -v node)")
MODEL=deepseek/deepseek-v4.1-flash; MODE=ro
if [ "${1:-}" = "--write" ]; then MODE=rw; shift; fi
if [ "${1:-}" = "-m" ]; then [ $# -ge 2 ] || { echo "cc-worker: -m bir model ister" >&2; exit 2; }; MODEL=$2; shift 2; fi
[ $# -eq 1 ] && [ -n "$1" ] && [ "${1#-}" = "$1" ] || { echo 'kullanım: cc-worker [--write] [-m MODEL] "görev"' >&2; exit 2; }
HERE=$(pwd -P); ROOT=$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || echo "$HERE")
case "$ROOT/" in
  "$WT"/?*|"$PROJ"/?*|/tmp/?*) ;;
  *) echo "cc-worker: reddedildi, izinli bir klasör değil: $ROOT" >&2; exit 3;;
esac
if [ "$MODE" = rw ] && ! [[ "$ROOT" =~ ^"$WT"/cc-worker(-[0-9]{1,3})?$ ]]; then
  echo "cc-worker: --write sadece $WT/cc-worker veya cc-worker-NN içinde çalışır" >&2; exit 3
fi
case "$NODE" in /usr/*|"$LIB"/*) ;; *) echo "cc-worker: node sandbox'ta görünmez ($NODE); ~/.local/lib/nodejs altına kur" >&2; exit 5;; esac
[ -f "$CC_JS" ] || { echo "cc-worker: command-code yok (npm i -g command-code)" >&2; exit 5; }
[ -f "$KEY" ] && [ "$(stat -c %a "$KEY")" = 600 ] || { echo "cc-worker: anahtar dosyası yok ya da izni 600 değil: $KEY" >&2; exit 4; }
ARGS=(-p "$1" --skip-onboarding --trust --no-auto-update --no-session --max-turns 120 -m "$MODEL")
# Başsız (headless) yazma ve komut için --yolo gerekir. Sınırları bayrak değil sandbox ve salt okunur kural dosyası koyar.
if [ "$MODE" = rw ]; then ARGS+=(--yolo --max-turns 400); fi
exec 3<"$KEY" </dev/null   # görev argümanla gelir; stdin kullanılmaz
exec "$LIB/cc-worker/sandbox.sh" "$MODE" "$ROOT" "$HERE" "$NODE" --require "$LIB/cc-worker/key-fd.cjs" "$CC_JS" "${ARGS[@]}"
```

```bash file=~/.local/lib/cc-worker/sandbox.sh
#!/usr/bin/env bash
# Allowlist sandbox: sandbox.sh ro|rw ROOT WORKDIR komut...   İçeride sadece işletim sistemi, program dosyaları ve ROOT var.
# rw: .git salt okunur (yeniden yazılmış bir .git işaretçisi gözden geçirenin git'inde kod çalıştırabilir) ve işçinin
# izin dosyası salt okunur bağlanır.
set -euo pipefail
MODE=$1; ROOT=$2; WORK=$3; shift 3
H=$HOME; mkdir -p "$H/.commandcode"
if [ "$MODE" = rw ]; then
  mkdir -p "$ROOT/.commandcode"
  REPO=(--bind "$ROOT" "$ROOT" --ro-bind "$H/.local/lib/cc-worker/worker-settings.json" "$ROOT/.commandcode/settings.local.json")
  if [ -e "$ROOT/.git" ]; then REPO+=(--ro-bind "$ROOT/.git" "$ROOT/.git"); fi
else
  REPO=(--ro-bind "$ROOT" "$ROOT")
fi
DNS=(); if [ -e /mnt/wsl/resolv.conf ]; then DNS=(--ro-bind /mnt/wsl/resolv.conf /mnt/wsl/resolv.conf); fi
exec bwrap --die-with-parent --new-session --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try --clearenv \
  --setenv HOME "$H" --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv LANG C.UTF-8 --setenv TERM dumb --setenv NO_COLOR 1 \
  "${DNS[@]}" --ro-bind /usr /usr --ro-bind /etc /etc --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /run --dir "$H" \
  --ro-bind "$H/.local/lib" "$H/.local/lib" --bind "$H/.commandcode" "$H/.commandcode" \
  "${REPO[@]}" --chdir "$WORK" -- "$@"
```

```js file=~/.local/lib/cc-worker/key-fd.cjs
// API anahtarını devralınan 3 numaralı tanımlayıcıdan yükler, sonra kapatır. Anahtar exec ortamına hiç girmez, yani
// /proc/self/environ'da yoktur; anahtar dosyası da sandbox'ta yoktur. CLI onu process.env'de ister, bu yüzden her alt
// süreç anahtarsız başlatılır: ajanın çalıştırdığı komutlar anahtarı miras almaz.
const fs = require('fs'), cp = require('child_process');
const NAME = 'COMMAND_CODE_API_KEY';
const key = fs.readFileSync(3, 'utf8').trim();
fs.closeSync(3);
if (!key) { process.stderr.write('cc-worker: anahtar boş\n'); process.exit(4); }
process.env[NAME] = key;
const spawnChild = cp.ChildProcess.prototype.spawn;
cp.ChildProcess.prototype.spawn = function (options) {
  if (options && options.envPairs) options.envPairs = options.envPairs.filter((p) => !p.startsWith(NAME + '='));
  return spawnChild.call(this, options);
};
const clean = (options) => {
  const env = { ...((options && options.env) || process.env) };
  delete env[NAME];
  return { ...(options || {}), env };
};
const lastOptions = (args) => {            // (file, options) ya da (file, args, options); options isteğe bağlı
  const out = [...args];
  const i = out.length > 1 && out[out.length - 1] && typeof out[out.length - 1] === 'object' && !Array.isArray(out[out.length - 1]) ? out.length - 1 : -1;
  if (i >= 0) out[i] = clean(out[i]); else out.push(clean());
  return out;
};
for (const f of ['spawnSync', 'execSync', 'execFileSync']) {
  const real = cp[f];
  cp[f] = function (...args) {
    if (f === 'spawnSync' && args.length === 1) args.push([]);
    if (f === 'execFileSync' && args.length === 1) args.push([]);
    return real.apply(this, lastOptions(args));
  };
}
require('module').syncBuiltinESMExports();   // CLI bir ES modülü: import'ları da bu sarmalayıcıları görmeli
```

```json file=~/.local/lib/cc-worker/worker-settings.json
{
 "permissions": {
  "defaultMode": "dont-ask",
  "allow": ["Edit(**)", "Write(**)", "Shell"],
  "deny": [
   "WebFetch", "WebSearch",
   "Edit(.commandcode/**)", "Write(.commandcode/**)",
   "Edit(.git/**)", "Write(.git/**)"
  ]
 }
}
```

```bash file=~/.local/lib/cc-worker/check.sh
#!/usr/bin/env bash
# cc-worker sınırını model çağırmadan kanıtlar. Sadece ad ve PASS/FAIL yazar; hiçbir gizli bilgiyi yazdırmaz.
set -u
L=$HOME/.local/lib; WT=${LANE_WORKTREES:-$HOME/agent-lanes/worktrees}; W=${CHECK_W:-$WT/cc-worker-01}
KEY=$HOME/.config/command-code-worker/api-key; NODE=$(readlink -f "$(command -v node)"); pass=0; fail=0
ok(){ if [ "$2" = yes ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
[ -d "$W" ] || { echo "test worktree'si yok: $W  (lane new cc-worker-01 --from main, ya da CHECK_W=...)" >&2; exit 2; }
sb(){ local m=$1 c=$2; shift 2; "$L/cc-worker/sandbox.sh" "$m" "$W" "$W" /bin/bash -c "$c" _ "$@" 2>/dev/null; }
for p in "$HOME/.ssh" "$HOME/.claude" "$HOME/.config" "$HOME/.bashrc" "$HOME/ai" /mnt/c "$(dirname "$W")/../repo"; do
  [ "$(sb ro '[ -e "$1" ] && echo yes || echo no' "$p")" = no ] && ok "gizli: $p" yes || ok "gizli: $p" no
done
[ -n "$(sb ro 'ls -A | head -1')" ] && ok "kontrol: repo içeriden okunuyor" yes || ok "kontrol: repo içeriden okunuyor" no
[ -z "$(sb ro 'touch .cc-probe && echo wrote')" ] && [ ! -e "$W/.cc-probe" ] && ok "ro: repoya yazma engelli" yes || ok "ro: repoya yazma engelli" no
[ "$(sb rw 'touch .cc-probe && rm .cc-probe && echo yes')" = yes ] && ok "rw: kendi worktree'si yazılabilir" yes || ok "rw: kendi worktree'si yazılabilir" no
[ -z "$(sb rw 'echo x >> .git && echo wrote')" ] && ok "rw: .git salt okunur" yes || ok "rw: .git salt okunur" no
[ -z "$(sb rw 'git status >/dev/null 2>&1 && echo works')" ] && ok "rw: git içeride çalışmıyor" yes || ok "rw: git içeride çalışmıyor" no
[ -z "$(sb rw 'echo x >> .commandcode/settings.local.json && echo wrote')" ] && ok "rw: işçi kendi kurallarını değiştiremez" yes || ok "rw: işçi kendi kurallarını değiştiremez" no
n=$(sb ro 'ls /proc | grep -c "^[0-9]"'); [ "${n:-99}" -le 4 ] && ok "host süreçleri görünmüyor ($n)" yes || ok "host süreçleri görünmüyor ($n)" no
[ "$(sb ro 'getent hosts commandcode.ai >/dev/null && echo yes')" = yes ] && ok "kontrol: DNS çalışıyor" yes || ok "kontrol: DNS çalışıyor" no
if [ -f "$KEY" ]; then
  [ "$(stat -c %a "$KEY")" = 600 ] && ok "anahtar dosyası 600" yes || ok "anahtar dosyası 600" no
  exec 3<"$KEY"
  out=$("$L/cc-worker/sandbox.sh" ro "$W" "$W" "$NODE" --require "$L/cc-worker/key-fd.cjs" -e '
const fs=require("fs"),cp=require("child_process");const k=process.env.COMMAND_CODE_API_KEY||"";
const env=fs.readFileSync("/proc/self/environ","latin1");
let fd="fd-closed";try{fs.fstatSync(3);fd="fd-open"}catch{}
const child=cp.execSync("printenv COMMAND_CODE_API_KEY || echo none").toString().trim();
console.log([k.length>8?"loaded":"missing",env.includes(k)||env.includes("COMMAND_CODE")?"in-environ":"not-in-environ",fd,child==="none"?"child-clean":"child-LEAK"].join(" "))' 2>/dev/null)
  exec 3<&-
  [ "$out" = "loaded not-in-environ fd-closed child-clean" ] && ok "anahtar: kullanılabilir; environ'da yok, fd kapalı, alt süreçte yok" yes || ok "anahtar: [$out]" no
else
  ok "anahtar dosyası var ($KEY)" no
fi
echo "$pass/$((pass+fail))"; [ "$fail" -eq 0 ]
```

```bash
chmod +x ~/.local/bin/cc-worker ~/.local/lib/cc-worker/*.sh
```

Test worktree'si açılınca: `~/.local/lib/cc-worker/check.sh` → `17/17`. Worktree'lerde `.commandcode/` klasörü oluşur; ana repoda `.gitignore`'a ekle (Python projelerinde `__pycache__/` da, yoksa `lane run` onları "listelenmemiş değişiklik" diye raporlar).

## 9. agent-lanes (`lane`)

`lane` küçük bir Python aracı (sadece standart kütüphane, bağımlılık yok). Repo bağlantısını sana ayrıca gönderiyoruz.

```bash
cd ~ && git clone <AGENT_LANES_REPO_URL> agent-lanes-tool
cd agent-lanes-tool && python3 -m unittest discover -s agent_lanes      # tüm testler geçmeli
ln -s "$PWD/agent_lanes/lane.py" ~/.local/bin/lane
lane --help
```

Ayarlar ortam değişkeniyle (varsayılanlar bu rehberin düzeniyle aynı, bir şey yapmana gerek yok):

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `LANE_ROOT` | `~/agent-lanes` | Ana klasör |
| `LANE_MAIN_REPO` | `$LANE_ROOT/repo` | Lane'lerin dallandığı repo |
| `LANE_WORKTREES` | `$LANE_ROOT/worktrees` | Worktree'ler (wrapper'lar da bunu okur) |
| `LANE_STATE_DIR` | `$LANE_ROOT/lanes` | Defter, loglar, raporlar: worktree dışında, işçi taklit edemez |
| `LANE_OC_BIN` / `LANE_CC_BIN` | `oc-worker` / `cc-worker` | `oc-worker-N` / `cc-worker-N` lane'leri için işçi komutu |

Projen başka yerdeyse: `export LANE_MAIN_REPO=~/ai/projects/<konu>` (`.bashrc` en üstüne ya da proje CLAUDE.md'sinde Claude'a söyle). Ya da ana repoyu `~/agent-lanes/repo` olarak klonla.

**Bir lane, baştan sona:**

```bash
lane new oc-worker-01 --from main --branch agent/oc-worker-01-parser   # worktree + dal + defter kaydı
~/.local/lib/oc-worker/check.sh                                          # ilk seferde: sandbox testi
nano /tmp/brief.md                                                       # BAĞLAM / GÖREV / GÜVENLİK / KABUL
lane run oc-worker-01 /tmp/brief.md                                      # bekler; işçi bitince çıkar
cat ~/agent-lanes/worktrees/oc-worker-01/.lane/REPORT.md
git -C ~/agent-lanes/worktrees/oc-worker-01 diff main                    # ÇALIŞTIRMADAN ÖNCE OKU
lane mutants oc-worker-01                                                # değişen satırlara mutant önerisi
lane accept oc-worker-01 --test "python3 -m pytest -q" --mutants mutants.json
lane merge oc-worker-01 -m "parser eklendi" --test "python3 -m pytest -q"
lane ls                                                                  # defter
lane gc                                                                  # kuru çalıştırma: ne silinebilir
```

Bunları genelde sen yazmazsın: Claude'a "bunu iki oc-worker lane'ine böl ve yaptır" dersin, skill (bölüm 17) Claude'a bu akışı öğretir.

**Brief şablonu:**

```text
BAĞLAM   - kod ne, yollar, NEYE DOKUNULMAYACAK (diğer lane'lerin dosyaları, yapılandırma)
GÖREV    - tam davranış, arayüzler, dosya adları
GÜVENLİK - kesin kurallar: hata durumunda kapalı kal, kodda ağ çağrısı yok, yıkıcı her şey varsayılan olarak kuru çalıştırma
KABUL    - sayılar: test sayısı, olması gereken adlı durumlar, gözden geçirenin tekrar koşacağı komutlar,
           ve "gözden geçiren testlerini mutasyona uğratacak"
```

`lane run` brief'in sonuna sonuç sözleşmesini ekler: işçi `.lane/LANE_RESULT.json` (durum, özet, değişen dosyalar, test sayıları, iddialar, engeller) ve en fazla 60 satır `.lane/REPORT.md` yazmak zorunda. Claude 30 satır okur, bütün transkripti değil; sonra her sayıyı kendisi doğrular.

**Mutant nedir:** işçinin kodunda bilerek bir hata (bir güvenlik kontrolünü silmek, `<`'yi `<=` yapmak, zorunlu bir argümanı kaldırmak). İşçinin testleri bu hatayı yakalamıyorsa testler yetersizdir. `mutants.json` örneği:

```json
[
  {"name": "boş girdi kontrolünü kaldır", "file": "src/parser.py",
   "find": "    if not rows:\n        raise ValueError", "replace": "    if False:\n        raise ValueError"},
  {"name": "sınırda bir fazla", "file": "src/parser.py",
   "find": "if count > limit:", "replace": "if count >= limit:"}
]
```

**herdr'da izlemek.** `lane run` işçiyi başsız çalıştırır. İzlemek için herdr'da `lanes` adlı bir workspace aç ve her lane için bir sekmede logu takip et: `tail -f ~/agent-lanes/lanes/oc-worker-01/run-*.log`. (Bizim kendi sürümümüzde `lane run --herdr` işçiyi doğrudan herdr sekmesinde açıyor ve bitince sekmeyi "bitti" yapıyor; Heeler de push atıyor. Açık kaynak sürümde bu yok; istersen Claude'a ekletebilirsin, ama kural: **herdr soketi asla sandbox'a girmez**, sekmeyi sandbox dışındaki `lane` yönetir.)

**Kurallarımız:**

- Aynı anda yaklaşık 5 lane. Paralel lane'ler **dosya bazında** bölünür; ortak arayüz iki brief'e de yazılır.
- Diff'i okumadan hiçbir şey çalıştırma: `accept`, işçi kodunu sandbox **dışında** çalıştırır.
- Her zaman mutasyon: 3-6 mutant. İşçinin "test edildi" iddiası birden çok kez bir mutantta çöktü.
- Mutant koşusu bitmeden commit mesajına sayı yazma (bir kere 10/10 yazıp 8/10'a düzeltmek zorunda kaldık).
- İşçiler commit atmaz, merge etmez, kimlik bilgisi almaz. PASS'ten sonra commit'i Claude atar.
- Lane'leri döngüyle yoklayarak bekleme; `lane run`'ı arka planda başlat, bitince haber gelir.
- `lane gc --apply` sadece sen onaylayınca.

## 10. Telefon: Windows sshd + WSL sshd + Heeler

Hedef: telefondan PC'deki herdr'a bağlanmak. Yol: telefon → (Tailscale) → Windows OpenSSH (port 22, sadece tailnet) → atlama (jump) → WSL içindeki kullanıcı sshd'si (`127.0.0.1:2222`) → herdr. Neden iki sshd: Windows sshd'ye doğrudan giriş Windows kabuğuna düşer; WSL'e geçmek için zorunlu komut (`command=wsl.exe ...`) kullanmak SSH çoğullamasıyla (ControlMaster) bozuluyor: sonraki oturumlar ilk komutu tekrar çalıştırıyor. Jump host ise temiz ve doğrudan Linux'a iner.

**10.1 WSL içinde kullanıcı sshd'si (2222, sadece localhost):**

```bash
sudo apt install -y openssh-server
sudo systemctl disable --now ssh.service ssh.socket      # sistem sshd'si (22) WSL'de gerekmiyor
mkdir -p ~/.local/opt/wsl-sshd ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
ssh-keygen -t ed25519 -N '' -f ~/.local/opt/wsl-sshd/ssh_host_ed25519_key
cat > ~/.local/opt/wsl-sshd/sshd_config <<EOF
# Heeler (telefon) için yetkisiz sshd: sadece localhost; Windows sshd üzerinden ProxyJump ile ulaşılır.
ListenAddress 127.0.0.1
Port 2222
HostKey $HOME/.local/opt/wsl-sshd/ssh_host_ed25519_key
PidFile $HOME/.local/opt/wsl-sshd/sshd.pid
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers $USER
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
StrictModes yes
X11Forwarding no
AllowAgentForwarding yes
AllowTcpForwarding local
AllowStreamLocalForwarding local
PermitTunnel no
Subsystem sftp /usr/lib/openssh/sftp-server
LogLevel INFO
EOF
```

```ini file=~/.config/systemd/user/wsl-sshd.service
[Unit]
Description=Yetkisiz sshd, 127.0.0.1:2222: Heeler (telefon) ile bu PC'deki herdr'a erişim

[Service]
ExecStart=/usr/sbin/sshd -D -e -f %h/.local/opt/wsl-sshd/sshd_config
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now wsl-sshd
systemctl --user status wsl-sshd        # active (running); "login records" uyarısı normal (root değil)
```

**10.2 Windows OpenSSH Server.** **Yönetici** PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
# Güvenlik duvarı: sshd sadece "Private" ağ profilinde açık. Tailscale arayüzünü Private yap.
Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -Profile Private
Get-NetConnectionProfile                                  # Tailscale satırının adını gör
Set-NetConnectionProfile -InterfaceAlias Tailscale -NetworkCategory Private
```

Ev Ethernet/Wi-Fi'n "Public" kalsın; o zaman sshd sadece tailnet'ten erişilebilir. (Ev ağın Private ise sshd evden de erişilir; anahtar-zorunlu olduğu için sorun değil ama bil.)

**Anahtarlar.** Windows kullanıcın **yönetici** ise Windows anahtarları `C:\ProgramData\ssh\administrators_authorized_keys` dosyasına gider (kullanıcı klasöründeki `authorized_keys` yöneticiler için okunmaz!). Yönetici değilse `C:\Users\<WINDOWS_KULLANICI>\.ssh\authorized_keys`. Yönetici durumu için dosya izni şart:

```powershell
icacls.exe "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

Anahtarlar çalıştığını gördükten **sonra** şifre girişini kapat: `C:\ProgramData\ssh\sshd_config` dosyasını önce yedekle (`copy sshd_config sshd_config.bak-YYYYMMDD`), sonra şunları ayarla ve `Restart-Service sshd`:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
```

**10.3 Heeler.** App Store'dan **Heeler** (iPhone, iPad, Mac). Uygulamada yeni host:

| Alan | Değer |
|---|---|
| Host | `127.0.0.1` |
| Port | `2222` |
| Kullanıcı | WSL kullanıcın |
| Jump Host | `<PC_TAILNET_ADI>`, port 22, kullanıcı `<WINDOWS_KULLANICI>` |
| Anahtar | Heeler'ın cihazda ürettiği anahtar |
| Backend | Herdr TUI |

Heeler'ın **açık anahtarını** iki yere ekle: Windows tarafı (yukarıdaki dosya) ve WSL `~/.ssh/authorized_keys`. Her cihazın (telefon, iPad, Mac) kendi anahtarı olsun. Anahtarları **gövdesine** göre eşleştir, etiketine göre değil: iki cihaz aynı etiketle ("heeler") anahtar üretebiliyor.

**10.4 Heeler eklentisi (push bildirimi).** WSL'de, herdr içinde (Node 20+ gerekir):

```bash
herdr plugin install ZingerLittleBee/Heeler --ref plugin --yes
herdr plugin list                         # heeler: enabled
herdr plugin action list                  # eşleştirme (QR) eylemini bul ve çalıştır, telefonla QR'ı okut
herdr plugin log list --plugin heeler     # sorun olursa
```

Bildirimler uçtan uca şifreli bir aktarma sunucusu üzerinden gelir. Bir pane "bitti" olunca (Claude cevabı bitirince, işçi bitince) telefona push gelir.

**Heeler'da bilmen gerekenler:** canlı terminal bağlantısı pane başına **tektir** (telefondan bağlanınca PC'deki görünüm "taken over" olabilir); Chat görünümü paylaşılır. Telefonda düz bir SSH istemcisi de çalışır; Heeler herdr'ı tanıyan görünüm ve push'u ekler.

**PC'den PC'ye test** (Heeler'dan önce, WSL içinden):

```bash
ssh -J <WINDOWS_KULLANICI>@<PC_TAILNET_ADI> -p 2222 $USER@127.0.0.1 'herdr --version'
```

## 11. (İsteğe bağlı) ikinci makine

İkinci bir PC/laptop varsa: uzun CPU işleri (test matrisleri, backtest, grid) oraya gider, git ve merge ana PC'de kalır.

1. İkinci makineye 1-10 arasını kur (Claude Code şart değil; WSL, Node, işçiler, herdr, sshd yeter).
2. Ana PC'de `~/.ssh/config`:

```text
Host laptop
  HostName <LAPTOP_TAILNET_ADI>
  User <LAPTOP_WINDOWS_KULLANICI>
  IdentityFile ~/.ssh/id_ed25519_laptop
Host laptop-wsl
  HostName 127.0.0.1
  Port 2222
  User <LAPTOP_WSL_KULLANICI>
  ProxyJump laptop
  IdentityFile ~/.ssh/id_ed25519_laptop
```

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_laptop -N ''     # açık anahtarı laptop'ta iki yere ekle (10.2 + WSL)
ssh laptop-wsl 'command -v oc-worker lane herdr'           # PATH .bashrc en üstünde değilse burada boş döner
herdr machine add laptop-wsl --label laptop                # herdr kenar çubuğunda laptop görünür
```

3. agent-lanes: `~/.config/agent-lanes/hosts.toml`:

```toml
[hosts.laptop]
ssh = "laptop-wsl"
root = "~/agent-lanes/worktrees"
worker = "oc-worker"
nice = 19
max_lanes = 2
```

```bash
lane host check laptop && lane host setup laptop
lane run oc-worker-07 /tmp/brief.md --host laptop           # rsync ile gönderir, uzakta çalıştırır, sonucu çeker
lane queue work.json --hosts local,laptop --max 3
```

Uzak lane'ler `systemd --user` birimleri olarak `nice 19` ile çalışır; SSH kapansa da ölmez (düz `setsid nohup` ile başlatılan bir işin SSH kapanınca öldüğünü gördük).

**Mac için:** `brew install herdr` ya da kurulum betiği; Node `brew install node`; macOS'ta etkileşimsiz SSH PATH'i için `~/.zshenv` içine `eval "$(/opt/homebrew/bin/brew shellenv)"`. macOS'ta sshd: Sistem Ayarları → Genel → Paylaşma → Uzaktan Giriş. Şifreyle girişi kapatmak için `/etc/ssh/sshd_config.d/000-no-password.conf` içine `PasswordAuthentication no` ve `KbdInteractiveAuthentication no`. Mac'te bwrap yok: Mac'i işçi makinesi değil, ayrı bir proje makinesi olarak kullanıyoruz.

## 12. Yeniden başlatma sonrası rutin

1. WezTerm'i aç → `Alt+L` → **WSL Ubuntu-24.04**. Bu WSL girişi kullanıcı servislerini başlatır (linger kapalıysa).
2. Kontrol:

```bash
systemctl --user status herdr-server wsl-sshd     # ikisi de active
systemctl --user restart herdr-server wsl-sshd    # değilse
```

3. `herdr` ile bağlan. Workspace'ler ve sekmeler duruyor ama içlerinde çalışan programlar kapanmış olur.
4. Her workspace'te Claude'a dön: `claude -c` (son konuşma) ya da `claude -r` (seç). Command Code için `cmd -c` / `cmd -r`.
5. Yarım kalan lane'ler: `lane ls`; bitmemiş bir lane'i aynı brief'le yeniden `lane run` ile başlat.
6. Heeler bağlanmıyorsa: genelde sadece 1. adım unutulmuştur.

## 13. Güncelleme rutini

```bash
# WSL / Ubuntu
sudo apt update && sudo apt upgrade -y
# Claude Code kendini günceller; OpenCode ve Command Code:
npm i -g opencode-ai command-code
herdr update
lane --help >/dev/null && (cd ~/agent-lanes-tool && git pull && python3 -m unittest discover -s agent_lanes -q)
# Her işçi güncellemesinden sonra:
~/.local/lib/oc-worker/check.sh && ~/.local/lib/cc-worker/check.sh
herdr plugin update
```

Windows:

```powershell
winget upgrade                          # listeyi gör
winget upgrade --id <PAKET_ID> --exact  # tek tek
```

Bizim deneyimlerimiz:

- `winget upgrade --all` yerine tek tek: bazı kurucular haber vermeden **bilgisayarı yeniden başlattı**; WSL ve içindeki her şey kapandı.
- **WSL'in kendi güncellemesi** (`Microsoft.WSL`) WSL'i kapatır, yönetici yetkisi ister: en sona bırak, yönetici PowerShell'de çalıştır, sonra bölüm 12.
- Bazı paketler "install technology is different" der: eskisini kaldırıp yenisini `winget install` ile kur.
- `winget` SSH oturumu üzerinden "Access is denied" verir: uzaktaki Windows'u güncellemek için o makinede oturum aç.
- Canlı bir şey çalıştıran makinede (bot, sunucu) güncellemeyi o iş boştayken yap.

## 14. Güvenlik kuralları (kısa liste)

- herdr soketi **asla** bir işçi sandbox'ına girmez: soketi olan her şey her pane'e (Claude oturumu dahil) yazı yazabilir. herdr'ın OpenCode entegrasyonu da sandbox içinde kullanılmaz.
- İşçiler: kimlik bilgisi yok, git yok, kendi worktree'si dışında yazma yok, commit/merge yok. Wrapper değişince `check.sh`.
- Gizli bilgiler (API anahtarları, `.env`, SSH anahtarları) repo dışında, `0600`. Repoya sadece üçüncü bir tarafa göstereceğin şeyi koy.
- Tüm sshd'ler: sadece açık anahtar; şifre ve keyboard-interactive kapalı. Windows sshd sadece Private profil (tailnet).
- Uzak bir dosyayı değiştirmeden önce yanına tarihli yedek: `dosya.bak-YYYYMMDD-neden`. Geri alma adımını yaz.
- Bir sembolik bağı değiştirmeden önce `command -v` ve `readlink -f` ile neyi değiştirdiğine bak (bir kere sistemdeki Node'u farkında olmadan başka sürümle ezdik).
- `git push`, yayınlama, dışarıya bir şey gönderme: Claude sormadan yapmaz (settings.json `soft_deny`).
- Canlı/üretim işleri (bot, sunucu) herdr içinde çalıştırılmaz; ayrı servis olarak çalışır.

## 15. Sorun giderme

| Belirti | Sebep | Çözüm |
|---|---|---|
| İşçi "Unable to connect to the API", çıkış kodu 6, `EAI_AGAIN` | Sandbox'ta DNS yok | `/mnt/wsl/resolv.conf` bağlı mı (sandbox.sh'de var); `check.sh` DNS satırı |
| OpenCode "database is locked" | Paralel çalıştırmalar tek SQLite'ı paylaşıyor | Her çalıştırmaya ayrı veri klasörü (sandbox.sh'de var) |
| OpenCode "Insufficient balance" | Yanlış sağlayıcı öneki | Go aboneliği: `opencode-go/...`; konsolda bölge Global |
| herdr pane'inde `powershell.exe: command not found` | systemd servisi Windows PATH'ini almıyor | `herdr-server-start` (bölüm 5) |
| `ssh pc 'lane ls'` → `command not found` | PATH `.bashrc`'de etkileşim korumasının altında | PATH satırını `.bashrc`'nin en üstüne taşı |
| Yeniden başlatma sonrası Heeler bağlanamıyor | Linger kapalı, kimse WSL'e girmedi | WezTerm'de WSL aç (bölüm 12) ya da `loginctl enable-linger` |
| Windows sshd anahtarı kabul etmiyor | Yönetici kullanıcı, anahtar yanlış dosyada / izin yanlış | `administrators_authorized_keys` + `icacls` (bölüm 10.2) |
| Bir betikte `pkill -f desen` çağıranı da öldürüyor (çıkış 144) | Desen çağıran komut satırında da geçiyor | Önce PID topla, PID ile öldür; ya da desene ikilinin tam yolunu koy |
| SSH ile ikinci oturum ilk komutu tekrar çalıştırıyor | Windows sshd'de zorunlu komut + ControlMaster | Zorunlu komut yerine jump host (bölüm 10) |
| `@dosya` import'u yüklenmiyor | Proje dışına çıkan import sessizce atlanıyor | Ortak bilgiyi `~/.claude/rules/` altına koy |
| `lane accept` PASS ama sıfır test | Test komutu verilmedi / sayılar okunamadı | Her zaman `--test` ver; rapordaki sayıları oku |
| İşçi "testler geçti" diyor, gerçek derleme kırık | İşçi sandbox'ında derleme/araç eksik olabilir | Kabulü her zaman kendi ortamında `lane accept` ile yap |

## 16. Kopya kağıdı

```bash
# --- paket kurulumu: KURULUM.md + extract.py aynı klasörde ---
python3 extract.py                      # KURULUM.md'deki file= bloklarından scripts/home/ üretir
cp -rv scripts/home/. ~/ && chmod +x ~/.local/bin/oc-worker ~/.local/bin/cc-worker ~/.local/bin/herdr-server-start \
  ~/.local/lib/oc-worker/*.sh ~/.local/lib/oc-worker/jail-shell ~/.local/lib/cc-worker/*.sh

# --- herdr ---
herdr                                   # bağlan
systemctl --user restart herdr-server   # sunucuyu yeniden başlat
herdr workspace list                    # workspace'ler

# --- işçiler ---
oc-worker "soru"                        # salt okunur
oc-worker --write "görev"               # sadece oc-worker-NN worktree'sinde
cc-worker [--write] "görev"
~/.local/lib/oc-worker/check.sh         # 17/17
~/.local/lib/cc-worker/check.sh         # 17/17

# --- lane ---
lane new oc-worker-01 --from main --branch agent/oc-worker-01-<etiket>
lane run oc-worker-01 brief.md
lane accept oc-worker-01 --test "<komut>" --mutants mutants.json
lane merge oc-worker-01 -m "<mesaj>" --test "<komut>"
lane ls ; lane gc

# --- Claude / Command Code oturumları ---
claude -c ; claude -r ; cmd -c ; cmd -r
```

## 17. Claude talimat dosyaları

Pakette `CLAUDE.md` adlı ayrı dosya olarak da var. İçinde dört parça:

1. **Kurulum asistanı talimatı:** yeni makinede ilk Claude oturumuna yapıştır; Claude bu rehberi adım adım, onayınla uygular.
2. **`~/.claude/CLAUDE.md` şablonu:** her oturum için genel kurallar.
3. **`~/.claude/rules/hosts.md` şablonu:** makine bilgileri.
4. **Proje `CLAUDE.md` şablonu** ve **`~/.claude/skills/lane/SKILL.md`** (lane skill'i).

İlk kurulumda en kolay yol: WSL + Node + Claude Code'u (bölüm 1, 2 ve 6'nın ilk satırları) elle kur, sonra bu klasörde `claude` başlat ve "CLAUDE.md'deki Kurulum asistanı talimatını uygula" de.
