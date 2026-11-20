<div align="center">

# LanDrop

**PCとスマホのあいだで、ブラウザだけでファイルを送り合う。**<br />
同じWi-Fiの中だけで動く、スマホにアプリを入れなくてよいファイル転送アプリ。

<p>
  <a href="#インストール">
    <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-0F766E?style=for-the-badge&labelColor=302D41" />
  </a>
  <a href="#インストール">
    <img alt="PC: Windows | macOS" src="https://img.shields.io/badge/PC-Windows%20%7C%20macOS-0F766E?style=for-the-badge&labelColor=302D41" />
  </a>
  <a href="#実機確認チェックリスト">
    <img alt="Phone: iPhone | Android" src="https://img.shields.io/badge/Phone-iPhone%20%7C%20Android-0F766E?style=for-the-badge&labelColor=302D41" />
  </a>
</p>

<p>
  <a href="#概要">概要</a>
  ·
  <a href="#インストール">インストール</a>
  ·
  <a href="#使い方">使い方</a>
  ·
  <a href="#セキュリティ">セキュリティ</a>
  ·
  <a href="#接続できないとき">接続できないとき</a>
  ·
  <a href="#開発">開発</a>
</p>

</div>

<hr />

## 概要

**LanDrop** は、PCで起動して、同じLANのスマホからブラウザで接続するファイル転送アプリです。iPhoneは **Safari**、Androidは **Chrome** でQRコードを読み取るだけで使えます。

PCでは **PCの画面** が開き、ファイルをドロップしてスマホへ送ったり、スマホから届いたファイルをサムネイルで見たりできます。インターネットを経由せず、CDNや外部フォントも使わないため、オフラインのLANでも動きます。

> 通信はHTTPで、**暗号化されません**。公共Wi-Fi・ホテル・カフェなど、知らない人と共有するネットワークでは使わないでください。

## インストール

### アプリにする（おすすめ）

Windowsは `LanDrop.exe`、macOSは `LanDrop.app` にまとめます。使うOSの上で一度だけ実行してください。

```powershell
# Windows（PowerShell）
cd "C:\path\to\LanDrop"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe packaging\build.py    # → dist\LanDrop.exe
```

```bash
# macOS（ターミナル）
cd ~/path/to/LanDrop
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python packaging/build.py             # → dist/LanDrop.app
```

できたアプリをダブルクリックすると、LanDrop自身のウィンドウでPCの画面が開きます。表示には、Windows 11に入っている WebView2（Edgeと同じエンジン）を使います。ターミナルのウィンドウは開きません。記録は `~/LanDrop/LanDrop.log` に残り、起動できないときは理由をダイアログで知らせます。

> 初めて起動したときにファイアウォールの確認が出たら、**プライベート ネットワーク** だけを許可してください。LanDrop が設定を自動で変えることはありません。macOS版のアプリは、まだ実機で確かめていません。

### Pythonから起動する

```powershell
# Windows（PowerShell）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m landrop
```

```bash
# macOS（ターミナル）
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m landrop
```

Python 3.11 以上が必要です（3.11 と 3.15 でテスト済み）。`pip install pywebview` も入れると、アプリと同じウィンドウで開きます。入れていなければ、Edge／Chrome のアプリ用のウィンドウで開きます。WSL2 を使っているWindowsでも、WSLの中ではなくWindows側で起動してください。

## 使い方

| 手順 | すること |
| --- | --- |
| **1. 起動** | アプリをダブルクリックする（または `python -m landrop`）。PCの画面にQRコードが出る |
| **2. 接続** | スマホのカメラでQRコードを読み取る。読めなければ「URLをコピー」で写したURLを入力する |
| **3. スマホへ送る** | PCの画面にファイルをドロップする。スマホの「受け取る」に数秒で出るので、タップして保存する |
| **4. PCへ送る** | スマホの「送る」で複数のファイルを選ぶ。PCの画面の「届いたファイル」にサムネイル付きで並ぶ |
| **5. 終了** | 「停止」を押すか、PCの画面を閉じる。30分間操作がなければ自動で停止する |

共有フォルダは `~/LanDrop/share`、受信フォルダは `~/LanDrop/received` です（Windowsでは `C:\Users\<ユーザー名>\LanDrop\…`）。QRコードのURLは起動のたびに変わります。

### 2回目からはQRコードなしで開く

一度QRコードで接続したスマホは、PCが覚えておきます。次にLanDropを起動したときは、スマホでLanDropのページを開くだけでつながります。

1. 初めての接続のあと、スマホのページは `http://landrop-<PC名>.local:8765` のような **名前のアドレス** に自動で移ります。この名前で開けないスマホでは、IPアドレスのまま使います。
2. 案内に従って、そのページを **ホーム画面に追加** します（iPhone: 共有ボタン →「ホーム画面に追加」、Android: ︙ メニュー →「ホーム画面に追加」）。
3. 次からは、PCでLanDropを起動してから、ホーム画面のアイコンをタップします。

ポートは前回と同じ番号を使います。名前のアドレスで開いていれば、ルーターがPCのIPアドレスを変えても開けます。30日間使わなかったスマホは自動で忘れます。PCの画面の「覚えているスマホ」で個別に解除でき、`--forget-devices` を付けて起動するとすべて忘れます。

## 機能

| 機能 | 内容 |
| --- | --- |
| **PCの画面** | LanDrop自身のウィンドウ（WebView2）で、QRコード・共有・届いたファイルを扱う |
| **送信** | 1ファイルずつ順番に送り、進捗を表示する。失敗したものだけ再送できる |
| **同じ名前** | 上書きせず、`写真 (1).jpg` のように自動で改名する |
| **大容量** | `.part` に少しずつ書くので、全体をメモリに読み込まない（上限10GB） |
| **自動停止** | PCの画面を閉じるか、30分間操作がないと停止する（転送中は止めない） |
| **端末を覚える** | 一度接続したスマホは、次の起動からQRコードなしで開ける。名前のアドレス（mDNS）でIPアドレスの変化にも対応する |
| **デザイン** | スマホとPCの画面で同じデザインシステムを使い、ダークモードにも対応 |

## 起動オプション

| オプション | 説明 |
| --- | --- |
| `--ip` | 待ち受けるLANのIPv4アドレス（既定: VPNや仮想アダプターを除いて自動で選ぶ） |
| `--port` | 待ち受けるポート（既定: 前回と同じ番号。初めては `8765`。使用中なら空きを探す） |
| `--share` | スマホへ渡すファイルのフォルダ（既定: `~/LanDrop/share`） |
| `--received` | スマホから届いたファイルの保存先（既定: `~/LanDrop/received`） |
| `--max-size` | スマホから受け取る1ファイルの上限（既定: `10GB`。`500MB` などで指定） |
| `--timeout` | 無操作で自動停止するまでの分数（既定: `30`。`0` で止めない） |
| `--no-open` | 起動時にPCの画面を開かない |
| `--forget-devices` | 覚えているスマホをすべて忘れてから起動する |

アプリに付けるときは、ショートカットのプロパティの「リンク先」の最後に `--ip 192.168.1.10` のように足します。

## セキュリティ

| 対策 | 内容 |
| --- | --- |
| **トークン** | 起動ごとに `secrets.token_urlsafe(32)` で作り、`compare_digest` で照合する |
| **Cookie** | HttpOnly・SameSite=Lax。発行したら、トークンのないURLへリダイレクトする |
| **端末の鍵** | 覚えたスマホには端末ごとの鍵をCookieで渡し、`~/LanDrop/devices.json` にはSHA-256のハッシュだけを残す。30日間使わなければ無効 |
| **名前のアドレス** | 名前のアドレスへ移るときは、2分間・1回だけ使える鍵でCookieを渡し直す |
| **未認証** | ファイル名・フォルダ構成・PC名を返さない。ログにトークンを残さない |
| **待ち受け** | LANのアドレスだけ。Hostヘッダーはそのアドレス、名前のアドレス、`localhost` だけ許す |
| **PCの画面** | `127.0.0.1` だけで、別のトークンと SameSite=Strict のCookieを使う |
| **ヘッダー** | CORSなし、`Origin` を確認、`nosniff`・CSP、ダウンロードは `attachment` |
| **パス** | パス要素を除き、フォルダの直下か確かめる。リンクと隠しファイルは対象外 |
| **ファイル名** | NFCに正規化し、`CON` などの予約名・使えない文字・末尾の `.` を直す |
| **サイズ** | 上限を超えるもの、空き容量が256MBを切りそうなものは受け付けない |
| **作らない** | 削除API、任意のパスを指定するAPI、届いたファイルを開く操作 |

QRコードやURLを知っている人は誰でも接続できます。他の人に見せないでください。停止すると、そのURLは使えなくなります。

覚えたスマホの鍵も、通信が暗号化されないため、同じWi-Fiで通信を盗み見られると使われるおそれがあります。また、同じLANの機器は名前のアドレスに偽の応答を返せます。信頼できるネットワークでだけ使い、使わなくなったスマホはPCの画面で解除してください。

## 接続できないとき

| 確かめること | 対処 |
| --- | --- |
| **同じWi-Fiか** | スマホとPCが同じルーターか。スマホがモバイル通信やVPNになっていないか |
| **端末間の遮断** | ゲスト用のSSIDや「プライバシーセパレーター」をやめ、通常のSSIDにつなぐ |
| **ファイアウォール** | LanDrop（または Python）の受信を「プライベート」でだけ許可する |
| **パブリック** | 注意が出たら、信頼できる場所に限りネットワークを「プライベート」にする |
| **アドレス** | 上部のアドレスがスマホと同じネットワークか。違えば `--ip` で指定する |
| **画面が開かない** | `~/LanDrop/LanDrop.log` にある「PCの画面」のURLをブラウザで開く |
| **接続が切れた** | PCでLanDropが動いているか確かめる。「QRコードを読み取ってください」と出たら、登録が解除されたか期限が切れたので、読み取り直す |
| **ホーム画面から開けない** | ポートが変わったか（PCの画面に注意が出る）、IPアドレスで追加していてPCのアドレスが変わった。QRコードを読み取り直し、ホーム画面に追加し直す |
| **名前のアドレス** | ファイアウォールの許可にUDP 5353（mDNS）も含まれているか。Androidは機種によって `.local` の名前を引けない |
| **Safari以外** | iPhoneの設定 > プライバシーとセキュリティ > ローカルネットワーク でオン |

## 開発

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

コードは `landrop/`、アプリにまとめる仕組みは `packaging/` にあります。

| ファイル | 役割 |
| --- | --- |
| **`cli.py`** | 起動オプション、アドレスの選択、停止の監視 |
| **`app.py`** | API（スマホ用とPCの画面用） |
| **`security.py`** | 認証、ヘッダー、PCの画面を `127.0.0.1` に限る処理 |
| **`storage.py`** | フォルダの一覧、`.part` を使った受信と改名 |
| **`filenames.py`** | ファイル名の安全化と `Content-Disposition` |
| **`network.py`** | LANのアドレスの検出とポートの確保 |
| **`idle.py`** | 無操作の判定と停止の依頼 |
| **`devices.py`** | 一度接続したスマホを覚える（端末の鍵、名前のアドレスへの受け渡し） |
| **`mdns.py`** | `landrop-<PC名>.local` の名前をLANに告知する |
| **`desktop.py`** | PCの画面を開く、エクスプローラー／Finderで表示する |
| **`static/`** | 画面。`ui.css` がデザインシステム（基本 → 意味 → 部品の3層） |

| API | 内容 |
| --- | --- |
| `GET /auth?t=<トークン>` | Cookieを発行して `/` へリダイレクトする（`h=<使い捨ての鍵>` は名前のアドレスへの受け渡し） |
| `POST /api/handoff` | 名前のアドレスで開き直すためのURLを返す（覚えた端末だけ） |
| `GET /api/files` | 共有フォルダのファイル一覧（`/api/info` はPC名など） |
| `GET /api/files/{ファイル名}` | ダウンロード |
| `POST /api/upload?name=<名前>` | 本文にファイルをそのまま送る（`Content-Length` が必要） |