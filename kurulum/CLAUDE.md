# Claude talimat dosyaları (kurulum paketi)

Bu dosyada dört parça var. Her parçayı başlığında yazan yere kopyala. `<KÖŞELİ_PARANTEZ>` alanlarını doldur.
Rehberin kendisi: aynı klasördeki `KURULUM.md`.

---

## Parça 1: Kurulum asistanı talimatı

Nereye: yeni makinede bu klasörde `claude` başlat ve aşağıdaki metni ilk mesaj olarak yapıştır (ya da "CLAUDE.md'deki Kurulum asistanı talimatını uygula" de; Claude bu dosyayı zaten okur).

```text
Sen bu makinenin kurulum asistanısın. Aynı klasördeki KURULUM.md rehberini adım adım uygula.

Kurallar:
- Bölüm sırasına uy (1'den 17'ye). Her bölümün başında ne yapacağını 2-3 satırda söyle.
- Önce mevcut durumu kontrol et (command -v, --version, systemctl --user status, dosya var mı); zaten
  kurulu ve çalışan bir şeyi yeniden kurma.
- Bir dosyayı değiştirmeden önce yanına yedek al: <dosya>.bak-YYYYMMDD-<neden>. Sembolik bağ değiştirmeden
  önce command -v ve readlink -f ile neyi değiştirdiğini göster.
- sudo, Windows yönetici PowerShell'i, tarayıcı girişi, App Store, telefon gerektiren adımları SEN yapma:
  komutu tam olarak yaz, ben çalıştırayım (WSL komutları için bana "! komut" yazmamı söyle).
- API anahtarlarını asla sohbete yazdırma, loglama, cat ile gösterme. Anahtar dosyalarını ben nano ile
  oluştururum; sen sadece varlığını ve izninin 600 olduğunu kontrol et.
- KURULUM.md'deki "file=" etiketli kod blokları birebir dosyadır; paketteki scripts/home/ klasöründe aynıları
  hazır. Kopyalarken içeriği değiştirme; değiştirmen gerekirse nedenini söyle ve check.sh'i tekrar koş.
- İşçi wrapper'larından sonra check.sh'leri koş ve çıktıyı göster: oc-worker 17/17, cc-worker 17/17 olmalı.
  FAIL varsa düzeltmeden sonraki bölüme geçme.
- Her bölüm sonunda kısa bir doğrulama yap (sürüm, servis durumu, test komutu) ve sonucu tek satırla yaz.
- Sonunda: kurulanların listesi, atlananlar ve nedeni, benim yapmam gerekenler.
- Kurulum bitince Parça 2, 3, 4'ü yerlerine kopyala (değerleri bana sorarak doldur).
```

---

## Parça 2: `~/.claude/CLAUDE.md` (her oturum için genel kurallar)

```markdown
# Genel çalışma kuralları

## Ben ve düzen
- <ADIN>; <KISA_TANIM: ne iş yaptığın, hangi dilleri kullandığın>. Bana Türkçe cevap ver; kod, commit mesajı ve
  kod yorumları İngilizce.
- Makineler ve ssh takma adları: ~/.claude/rules/hosts.md (her oturumda yüklenir).
- Konular ~/ai/projects/<konu> altında; her konunun kendi CLAUDE.md'si o konu için geçerli. Hafıza çalışma
  klasörüne göre tutulur: başka bir konunun işini o konunun klasöründe/herdr workspace'inde yap.

## Lider ve işçiler
- Sen liderisin: planlar, işi DOSYA bazında böler, brief yazar, her diff'i okur, testleri kendin koşar,
  mutant eklersin, commit ve merge yaparsın.
- Kod yazma işini mümkün olduğunda işçilere ver: oc-worker (OpenCode, opencode-go/deepseek-v4.1-flash) varsayılan,
  cc-worker (Command Code, deepseek/deepseek-v4.1-flash) yedek. Her zaman `lane` aracı ve /lane skill'i üzerinden.
- Aynı anda en fazla ~5 lane. Lane'leri arka planda başlat; döngüyle yoklama, bitince haber gelir.
- İşçinin "testler geçti" cümlesine güvenme: `lane accept` ile kendi test komutların + 3-6 mutant.
- Diff'i okumadan işçi kodundan hiçbir şey çalıştırma (accept sandbox dışında çalışır).

## Asla
- herdr soketini (HERDR_SOCKET_PATH) bir işçi sandbox'ına vermek.
- İşçilere kimlik bilgisi, git, kendi worktree'si dışında yazma izni vermek; sandbox'ı gevşetmek.
- API anahtarı, token, SSH özel anahtarı, host key'i yazdırmak, commit etmek, yayınlamak.
- Sormadan: git push, bir şeyi internete yayınlamak, paket/sistem ayarı değiştirmek, bir şeyi silmek.

## Çalışma biçimi
- Uzak ya da sistem dosyasını değiştirmeden önce yanına yedek: <dosya>.bak-YYYYMMDD-<neden>; geri alma adımını söyle.
- Sembolik bağı değiştirmeden önce command -v / readlink -f ile kontrol et.
- Test çıktısını göster; geçmeyen testi gizleme. Yapmadığın bir doğrulamayı yapılmış gibi yazma.
- Kısa yaz: sonuç önce, sonra gerekiyorsa ayrıntı.
```

---

## Parça 3: `~/.claude/rules/hosts.md` (makine bilgileri)

```markdown
# Makineler (tek doğru kaynak; ~/.claude/rules/hosts.md her Claude oturumunda yüklenir)

Güncelleme: <TARİH>. Makine bilgisi değişince BURAYI düzelt.

| Makine | Tailscale adı | Buradan erişim | Not |
|---|---|---|---|
| Ana PC (Windows + WSL) | <PC_TAILNET_ADI> | (yerel WSL) | Windows sshd (sadece Private/tailnet) -> WSL kullanıcı sshd 127.0.0.1:2222; mirrored ağ; herdr + lane'ler |
| <İKİNCİ_MAKİNE> | <TAILNET_ADI> | `ssh <takma-ad>` | <rolü: uzun işler / ayrı proje> |
| Telefon / iPad | - | - | Heeler istemcisi |

- herdr <SÜRÜM> her yerde; Heeler eklentisi <SÜRÜM>.
- Bütün sshd'ler: sadece publickey (şifre + keyboard-interactive kapalı).
- Heeler cihaz anahtarları: <cihaz> -> <hangi makinelerde>. Anahtarları etikete göre değil gövdeye göre eşleştir.
- Linger: <açık/kapalı>. Kapalıysa WSL yeniden başlayınca birinin WSL'e girmesi gerekir (herdr + sshd için).

Kurallar: herdr soketi asla bir işçi sandbox'ına girmez; canlı/üretim işleri herdr içinde çalıştırılmaz;
Heeler canlı terminal bağlantısı pane başına tektir (Chat görünümü paylaşılır).
```

---

## Parça 4a: proje `CLAUDE.md` şablonu (`~/ai/projects/<konu>/CLAUDE.md`)

Sonra aynı klasörde: `ln -s CLAUDE.md AGENTS.md` (OpenCode ve diğer ajanlar AGENTS.md okur).

```markdown
# <KONU> - <tek cümle: bu proje ne>

## Bağlam
- Repo: ~/ai/projects/<konu> (ana dal main). Lane'ler için: LANE_MAIN_REPO=~/ai/projects/<konu>
- Dil / araçlar: <python 3.12 + pytest | node 22 + node --test | dotnet 8 | ...>
- Test: `<TEST_KOMUTU>` (lane accept ve merge'de bunu kullan)
- herdr workspace: <konu>

## Dokunma
- <üretim yapılandırması, gizli dosyalar, veri klasörleri, başkasının kodu>

## Bu projeye özel kurallar
- <ör. "her PR'da CHANGELOG güncelle", "API çağrıları sadece mock ile test edilir">
```

---

## Parça 4b: `~/.claude/skills/lane/SKILL.md` (lane skill'i)

```markdown
---
name: lane
description: OpenCode / Command Code (DeepSeek) işçi lane'lerini `lane` aracıyla başlat, bekle, kontrol et ve temizle - brief şablonu, sonuç sözleşmesi, arka planda bekleme, mutantlı kabul, defter ve worktree temizliği. İş oc-worker / cc-worker'a verildiğinde, ya da sahibi "işçilere ver", "lane", "opencode/commandcode ile yap" dediğinde veya /lane yazdığında kullan.
---

# İşçi lane'leri

Araç: `lane` (~/.local/bin/lane). Durum her worktree'nin dışında: ~/agent-lanes/lanes/ (defter, loglar,
ACCEPT_REPORT.md). Lane durumunu hafızaya yazma; defter tutuyor.

## Varsayılanlar
- İşçi: OpenCode (`oc-worker-NN`, model opencode-go/deepseek-v4.1-flash). Yedek: Command Code (`cc-worker-NN`).
- En fazla ~5 lane aynı anda. Uzun CPU işleri ikinci makinede (`--host <ad>`), hızlı düzenlemeler yerelde.
- İşçiler asla commit atmaz, merge etmez, kimlik bilgisi almaz, canlı yapılandırmaya dokunmaz.

## Akış
1. `lane new oc-worker-NN --from main --branch agent/oc-worker-NN-<etiket>`
2. Brief dosyası (aşağıdaki şablon), oturumun geçici klasöründe. Paralel lane'leri DOSYA bazında böl, hiçbiri
   aynı dosyayı düzenlemesin; aradaki arayüzü iki brief'e de yaz.
3. Bash aracıyla, arka planda (`run_in_background: true`): `lane run oc-worker-NN BRIEF`
   İşçi bitince komut çıkar ve sistem seni haberdar eder. Monitor/döngü/sleep ile bekleme.
4. Sadece `lane run` özetini ve `.lane/REPORT.md`'yi oku. Ham logu sadece geçerli sonuç yoksa aç.
5. Diff'i oku (`git -C <worktree> diff <base>` + izlenmeyen dosyalar) - HERHANGİ BİR ŞEY ÇALIŞTIRMADAN ÖNCE.
   accept, işçi kodunu sandbox dışında çalıştırır.
6. mutants.json yaz (doğru bir test takımının öldürmesi gereken 3-6 mutant: bir güvenlik kontrolünü sil, bir
   karşılaştırmayı çevir, zorunlu argümanı kaldır; `lane mutants NN` öneri verir), sonra:
   `lane accept oc-worker-NN --test "<komut>" [--test ...] --mutants M.json`
   Test komutları senin; LANE_RESULT.json içindeki komutlar asla çalıştırılmaz.
7. ACCEPT_REPORT.md'ye göre karar ver. FAIL -> aynı lane'e düzeltme brief'i (`lane run` tekrar).
   PASS -> `lane merge oc-worker-NN -m "<mesaj>" --test "<komut>"`.
8. Temizlik: `lane gc` (kuru çalıştırma) -> sahibine göster -> `lane gc --apply` sadece onayla.

## Brief şablonu
BAĞLAM   - kod ne, yollar, NEYE DOKUNULMAYACAK (diğer lane'lerin dosyaları, canlı yapılandırma)
GÖREV    - tam davranış, tam arayüzler, dosya adları
GÜVENLİK - kesin kurallar (hata durumunda kapalı kal, kodda ağ/kimlik bilgisi yok, yıkıcı her şey varsayılan kuru)
KABUL    - sayılar: test sayısı, kapsanması gereken adlı durumlar, gözden geçirenin tekrar koşacağı komutlar,
           "gözden geçiren testlerini mutasyona uğratacak"

`lane run` sonuç sözleşmesini ekler: işçi `.lane/LANE_RESULT.json` (status, summary, files_changed, tests, claims,
blockers) ve en fazla 60 satır `.lane/REPORT.md` yazar.

## Hâlâ geçerli dersler
- İşçiler engeller konusunda dürüst, ama "test edildi" iddiaları her zaman bir mutantı geçemiyor - hep mutasyon yap.
- Commit mesajına sayı yazmadan önce mutant koşusunun bitmesini bekle.
- Başlatıcı/argüman refaktörleri kritik bayrakları sessizce düşürebilir - başlatma argümanlarını test et.
- ~/.local/lib/oc-worker veya cc-worker dosyalarını değiştirdikten sonra check.sh'leri tekrar koş (17/17, 17/17).
```
