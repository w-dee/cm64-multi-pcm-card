# CM-64 PCM カードのファームウェアと PC ツール

RP2040 と USB CDC を使い、カード上の **S29GL064S70TFI030（U4、x8、8 MiB）** を
読み書きします。ファームウェアを格納する W25Q128（U2、SPI/QSPI、16 MiB）とは別です。
起動・USB 接続だけではパラレルフラッシュを書き換えません。

カードを CM-64 から取り外し、USB 単独給電で使用してください。
HOST 検出、電源異常、USB 切断・サスペンド時にはアクセスを停止します。
消去・書き込み中の電源断は元の内容を復旧できません。最初にバックアップしてください。

赤 LED（GPIO20）はファームウェア動作中に常時点灯します。
緑 LED（GPIO19）はパラレルフラッシュへのアクセス中（型番確認・読み出し・消去・書き込み・検査）に
点灯し、終了・中断時に消灯します。合否は PC ツールの終了コードと表示で確認します。

## PC ツール

Python **3.10 以上**と pyserial を使用します。リポジトリのルートから実行します。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r firmware/requirements.txt
python3 firmware/pcm_card.py list
python3 firmware/pcm_card.py info
```

接続が 1 枚なら自動選択します。複数枚ある場合は `list` に表示される USB シリアル番号を
`--serial SERIAL` で指定してください。`--port /dev/ttyACM0` や `--port COM3` でも指定できます。
これらのオプションはサブコマンドの**前**に置きます。
Linux では CDC ポートへのアクセス権限が必要です。他のシリアル端末は閉じてください。

| サブコマンド | 内容 |
|---|---|
| `list` | 接続中のカードを一覧表示 |
| `info` | ファームウェア情報表示と NOR の JEDEC / CFI 照合 |
| `backup FILE` | 全 8,388,608 バイトを読み出して保存 |
| `restore FILE` | 全域を消去し、8,388,608 バイトのイメージを書き込み・照合 |
| `read-bank BANK FILE` | 指定バンクの 524,288 バイトを読み出して保存 |
| `write-bank BANK FILE` | 指定バンクだけを消去・書き込み・照合 |
| `test --force` | **全域を破壊的にテスト**。終了後にはテストパターンが残る |
| `bootsel` | UF2 更新用の RP2040 ROM BOOTSEL モードへ再起動 |

バンク番号は **0～15**、開始アドレスは `BANK × 0x80000` です。
バンク 0 は `0x000000–0x07ffff`、バンク 15 は `0x780000–0x7fffff` です。
イメージはコネクタ側の物理アドレス・データ順の raw binary で、ヘッダやバイト並べ替えはありません。
入力の長さは厳密に検査します。読み出し先の既存ファイルは上書きしません。
読み出しに失敗した場合は新規作成した不完全なファイルを削除します。

```sh
# バックアップ・生成物・ログを公開リポジトリから除外する保存先
mkdir -p firmware/local

# 全体を保存してから、必要なら同じ内容に復元
python3 firmware/pcm_card.py backup firmware/local/card-backup.bin
python3 firmware/pcm_card.py restore firmware/local/card-backup.bin

# 各バンクの読み書き（例: バンク 15）
python3 firmware/pcm_card.py read-bank 15 firmware/local/bank15.bin
python3 firmware/pcm_card.py write-bank 15 firmware/local/bank15.bin

# 全域を破壊的に検査し、バックアップから復元
python3 firmware/pcm_card.py test --force
python3 firmware/pcm_card.py restore firmware/local/card-backup.bin
python3 firmware/pcm_card.py backup firmware/local/card-restored.bin
cmp firmware/local/card-backup.bin firmware/local/card-restored.bin
```

`test` は `--force` がなければ、ポートを開く前に終了します。
テスト自体には自動バックアップ・自動復元を含みません。失敗・中断後もバックアップから復元してください。
書き込みはセクタ消去を伴い、指定範囲の元の内容を失います。バンク書き込みでは他のバンクを消去しません。
バンク 15 の上端は 8 KiB セクタ、それ以外は 64 KiB セクタとして扱います。

転送は 256 バイトごとのアドレス・CRC32、全転送の CRC32、終了時のバス禁止状態を検査します。
書き込みはデバイス側で各ページ全バイトを読み戻し、さらに PC 側でも対象範囲全体を再読出しして
入力と比較します。成功時は `OK`、ファイル転送時は SHA-256 も表示します。
終了コードは成功 `0`、通信・照合・ファイルエラー `1`、引数エラー `2`、中断 `130` です。
転送中の中断・エラーでは書き込み完了を保証しません。自動再試行は行いません。

## 全域テストの内容

1. JEDEC ID `01 / 7e / 10 / 01`、CFI、容量、バッファ、ブートセクタ構成を照合。
2. チップ消去し、全 8 MiB が `FF` であることを読み出して確認。
3. アドレス線の walking-one / walking-zero とデータ線の診断パターンを 68 点へ書き込み、
   昇順・逆順で 136 回照合。
4. 再消去・全域 `FF` 確認後、アドレス依存ハッシュパターンを**全バイト**に書き込み、全域を昇順で照合。
5. 再消去・全域 `FF` 確認後、反転パターンを**全バイト**に書き込み、全域を降順で照合。

全域の各ビットで 0 と 1 の双方を書き込み・読み出しします。PC 側でも件数、CRC、診断点数、合否を確認します。
長時間の電源断後の保持特性や CM-64 からの読み出しは検査しません。
同じ MCU 経路での往復検査では、一貫したアドレス線・データ線の入れ替わりを識別できない場合があります。

## ファームウェアのビルド

Pico SDK **2.3.1**（TinyUSB サブモジュールを含む）、Arm GNU ツールチェーン、CMake、Ninja が必要です。
SDK は別のディレクトリへ取得してください。

```sh
git clone --branch 2.3.1 --depth 1 --recurse-submodules \
  https://github.com/raspberrypi/pico-sdk.git "$HOME/pico-sdk"
export PICO_SDK_PATH="$HOME/pico-sdk"

cmake -S firmware -B firmware/build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build firmware/build -j 4
```

`arm-none-eabi-gcc` が PATH にない場合は、構成時に
`-DPICO_TOOLCHAIN_PATH=/path/to/toolchain`（その配下に `bin/` があるディレクトリ）を指定します。
UF2 作成用 picotool が未検出なら SDK が取得・ビルドします。既存の CMake パッケージを使用する場合は
`-Dpicotool_DIR=/path/to/picotool-cmake-package` を指定できます。
SDK のボード設定はこの基板専用で、12 MHz 水晶・16 MiB Winbond QSPI・UART 無効です。
`-Wall -Wextra -Werror` を有効にしています。

出力は `firmware/build/pcm_card.uf2` です。ELF・BIN 等も同じ場所に生成されます。
`build*/` と `local/` は Git 管理対象外で、ROM データやビルド済みファイルを同梱する必要はありません。

## RP2040 への書き込み

初回導入は J2 の BOOTSEL 手順で RP2040 内蔵 ROM を起動し、表示される `RPI-RP2` ドライブへ
`firmware/build/pcm_card.uf2` をコピーします。起動後は CDC ポートとして再接続されます。
動作中の本ファームウェアを更新する場合は次の操作で J2 の短絡を省略できます。

```sh
python3 firmware/pcm_card.py bootsel
# RPI-RP2 ドライブが現れてから pcm_card.uf2 をコピーする
```

USB 対応版 picotool とデバイスへのアクセス権限がある場合は、BOOTSEL モードで
`picotool load -v -x firmware/build/pcm_card.uf2` でも書き込み・照合・起動できます。
SPI/QSPI 上の RP2040 ファームウェアだけを更新し、U4 のデータは起動時に消去しません。
書き込み中の意図しない再起動を防ぐため、1200 baud リセットと USB vendor reset は無効です。
`bootsel` は処理中には受け付けません。

## 開発時の検証

```sh
python3 -m unittest discover -s firmware/tests -p 'test_*.py'
cc -std=c11 -Wall -Wextra -Werror -O2 firmware/tests/patterns_test.c -o /tmp/cm64-patterns-test
/tmp/cm64-patterns-test
```

ネイティブ C テストは全アドレスの 595 転送順、全域パターンの CRC、552 通りのアドレス故障モデル、
72 通りのデータ故障モデル、および全 16 バンクの消去セクタ境界を検査します。
Python テストは転送の分割受信、CRC・アドレス不一致、読み戻し不一致、強制指定、入力長、
既存バックアップの保護などを確認します。

### 実機確認（2026-11-14）

RP2040 / S29GL064S70TFI030 の実機 1 枚で、全 16 バンクへの個別パターンの書き込み・読み出し、
バンク 15 書き換え時の他バンク保持、不正なページ CRC の拒否、全域テスト（約 10 分）を確認しました。
テスト前の全体バックアップ 2 回は一致し、テスト後に元の内容を復元して全 8 MiB の一致を確認しています。
その後 LED を最終仕様へ変更し、更新後の GPIO 状態と全 8 MiB のデータ保持も確認しました。
バックアップ・実機ログ・検証結果 JSON は `local/` に保存し、公開ファイルには含めません。

## USB プロトコル

115200 baud / 8N1 / DTR 有効の CDC-ACM です。通信速度設定は USB 転送速度を制限しません。
ASCII・LF 区切りで 1 要求ずつ処理します。数値引数と DATA / PAGE / ACK の数値は 16 進です。
CRC32 は Python の `zlib.crc32` と同じ方式です。

| 要求 | 応答 |
|---|---|
| `INFO` | `INFO protocol=1 ...` と JEDEC / CFI 等、`END INFO OK ...` |
| `READ BASE SIZE` | `DATA ADDRESS HEX512 CRC32` を繰り返し、`END READ OK ...` |
| `WRITE BASE SIZE CRC32 CONFIRM` | 消去・FF 検査後に `READY WRITE` |
| `PAGE ADDRESS HEX512 CRC32` | 上記 WRITE 中だけ受理。順番・CRC・書込み・読戻し確認後に `ACK ADDRESS CRC32` |
| `TEST ERASE_ALL` | 診断ログ、`RESULT PASS ...`、`END TEST OK ...` |
| `BOOTSEL` | `END BOOTSEL OK` 後に再起動 |

BASE はバンク境界、SIZE は 1 バンク分または BASE=0 で全体のみ受理します。
最後の PAGE の ACK 後に `END WRITE OK ...` を返します。
通常の END 応答は `bytes=... crc=... ARM=0 PROG_EN_N=1 reason=none` を含み、
これらの件数は 10 進、CRC は 16 進です。失敗は `END ... FAIL` です。
PAGE は 10 秒以内に送信してください。切断・受信タイムアウト・異常時にはバスを解放します。
接続中の短い待機では電源供給が続くため、USB ケーブルを抜く前に END 応答を待ってください。

NOR 制御の参照資料:
[Infineon S29GL064S データシート Rev. K](https://www.infineon.com/assets/row/public/documents/10/49/infineon-s29gl064s-64-mbit8-mbyte3-datasheet-en.pdf)。
