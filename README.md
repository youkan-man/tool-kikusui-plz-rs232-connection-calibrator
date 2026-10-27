# KIKUSUI PLZ-U RS232 diagnostic tools

PLZ-UシリーズのRS232接続を、Linux上で安全に切り分けるための診断CLIです。
問い合わせには読み取り専用の `*IDN?` だけを使用し、負荷ON/OFF、設定変更、
リセット、校正値の書き込みは行いません。

このリポジトリにSandbox、HostHub、認証ブローカーなどの内製基盤への依存は
ありません。USB-RS232変換器が接続されたLinux上で直接実行できます。

## 対応環境

- Linux（`sysfs`、`procfs`を使用）
- Python 3.9以降
- pyserial 3.5以降
- USB変換器用カーネルドライバ（CH340は`ch341`、FTDIは`ftdi_sio`）

PLZ-Uの公式RS232設定は次のとおりです。

- 2400 / 4800 / 9600 / 19200 bps
- 8 data bits、parityなし、1 stop bit
- XON/XOFF
- LF終端
- PC接続にはクロス（ヌルモデム）ケーブル

仕様参照:

- [KIKUSUI PLZ-U Communication function](https://global.kikusui.co.jp/spec/plz-u_communication-function/)
- [KIKUSUI Wavy for PLZ-U（対応変換器とケーブル上の注意）](https://kikusui.co.jp/download/wavy-for-plz-u/)

## セットアップ

Ubuntuでは、必要なパッケージとドライバを次のスクリプトで準備できます。

```bash
./scripts/install-ubuntu-deps.sh
```

手動の場合は次のパッケージを導入してください。

```bash
sudo apt-get install python3-serial usbutils
sudo modprobe usbserial ch341 ftdi_sio
```

pip環境を使う場合は、リポジトリ直下でインストールできます。

```bash
python3 -m pip install -e .
```

## 基本的な使い方

USB構成、安定パス、権限、重複露出を確認します。

```bash
python3 -m plz_rs232_diag inventory
```

検出した全ポートへ9600/8N1/XON-XOFF/LFで5回ずつ問い合わせます。

```bash
sudo python3 -m plz_rs232_diag probe
```

USB情報、ポート所有者、疎通、UARTカウンタ、直近のカーネルイベントを
まとめて採取します。

```bash
sudo python3 -m plz_rs232_diag --output plz-report.json all
```

ポートは明示指定できます。再起動後に番号が変わる可能性があるため、実運用では
`/dev/serial/by-path/...`を推奨します。

```bash
sudo python3 -m plz_rs232_diag probe /dev/serial/by-path/pci-0000:01:00.6-usb-0:4:1.0-port0
```

## 詳細診断

公式対応ボーレートを走査します。

```bash
sudo python3 -m plz_rs232_diag scan-baud /dev/ttyUSB0
```

9600 bpsで、8N1/8N2/8E1/8O1/7E1/7O1とLF/CR/CRLFの18通りを走査します。

```bash
sudo python3 -m plz_rs232_diag scan-framing /dev/ttyUSB0
```

9600付近の実効ボーレートずれを確認します。

```bash
sudo python3 -m plz_rs232_diag scan-trim /dev/ttyUSB0
```

Linuxドライバが対応しているポートでは、送受信数とframe/parity/BREAK/overrun
カウンタの増分を採取できます。

```bash
sudo python3 -m plz_rs232_diag counters /dev/ttyUSB2
sudo python3 -m plz_rs232_diag break-check /dev/ttyUSB2
```

ポートを開いているプロセスと、USB関連の直近カーネルイベントは個別にも確認できます。

```bash
sudo python3 -m plz_rs232_diag owners
sudo python3 -m plz_rs232_diag kernel-log --since "30 minutes ago"
```

全コマンドはJSONを出力します。`--output`はサブコマンドより前に指定してください。
`probe`と`all`に`--strict`を付けると、全ポートが正常応答しない場合に終了コード2を
返すため、自動テストにも利用できます。

## 判定の見方

- `reachable`: `KIKUSUI,...`形式の正常応答
- `reply-with-uart-errors`: 返信はあるがframe/BREAK等が増加。RS232レベル変換、
  変換器、ケーブル、GND、信号品質を優先して確認
- `unexpected-reply`: バイトは届くが正常なIDNではない
- `transmitted-no-rx`: 問い合わせは送信できたが受信が0バイト
- `no-response`: 応答なし。カウンタ非対応ポートではTX/RXの厳密な切り分けは不可

現地で得られた初回調査結果は[docs/initial-findings.md](docs/initial-findings.md)にあります。

## テスト

ハードウェア不要の単体テストです。

```bash
python3 -m unittest discover -v
```
