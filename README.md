# Bioview

Bioview setup for NCS

基於 PyPI 上 `bioview` 0.9.2(作者 Aakash Kapoor,GPL-v3)的修改版,用於 Ettus USRP B210 的 MIMO 量測。
本 repo 可以直接用 `pip` 安裝,並附上啟動範例 `launch_bioview.py`。

## 與上游 0.9.2 的差異

- **三角波校正(Calibration probe)**:錄製開頭與結尾各插入一段 gated 三角波 AM 探測訊號(2 kHz × 5 週期,每 1 s 一次,兩個 Tx 輪流),
  用來評估各通道品質並計算 CVI / LUT。結果存成 `<檔名>_calibration.json`,錄製結束後跳出 start / end 對照視窗。
  - 分析器會在整段串流中自動找出 burst 的位置(Rx 緩衝造成的延遲每次不同),不需手動對齊。
  - 每個時段先送 100 ms 的未調變載波,確保每個 burst 前都有穩定的載波參考。
  - 視窗顯示每個通道中相關係數最高的 burst;曲線超出範圍時自動縮放並在標題註明,沒有可用 burst 的通道會明確標示。
- **Annotation**:文字檔(`.log`)與 `_marks.csv` 只在**第一筆標註**時才建立;沒有標註的錄製不會留下這兩個檔案。
  檔名沿用該次錄製 `.h5` 的名稱(例如 `example_2.h5` → `example_2.log`、`example_2_marks.csv`)。

## 在新電腦上安裝(Windows 10/11)

1. **Python 3.12 x64**
   到 [python.org](https://www.python.org/downloads/windows/) 下載 3.12 x64 安裝檔,勾選 *Add python.exe to PATH*(並保留 `pip`、`venv`)。
   本套件要求 Python `>=3.12,<3.13`,**3.13 不能用**。安裝後在命令提示字元確認:`python --version`
2. **UHD 4.8.0.0**
   到 <https://files.ettus.com/binaries/> 下載 `uhd_4.8.0.0-release` 的 Windows 安裝檔並安裝(建議勾選驅動與 FPGA images 下載器)。
   安裝完成後**重新開啟**命令提示字元或 PowerShell。

   > ⚠️ 本 repo 的 `pyproject.toml` 將 Python 版 `uhd` 套件**鎖定在 4.8.0.0**,以配合這裡安裝的驅動版本。
   > 若改裝其他版本的 UHD 驅動(例如 4.11.0.0),記得同步修改 `pyproject.toml` 裡的 `uhd` 版本並重新
   > `pip install`,否則會出現 `uhd.dll` 與 Python API 版本不一致的警告(見下方常見問題)。
3. **USB 驅動(B200 / B210)**
   接上 B210(USB 3.0 埠),用 [Zadig](https://zadig.akeo.ie/) 選擇 B200/B210 裝置並安裝 **WinUSB** 驅動。
4. **FPGA 映像檔與裝置測試**
   若安裝 UHD 時沒有勾選 FPGA images,請執行 `uhd_images_downloader`(或重跑安裝程式並勾選)。接著測試:
   ```bat
   uhd_find_devices
   uhd_usrp_probe
   ```
   `uhd_find_devices` 要能看到 B210。
5. **建立虛擬環境**
   ```bat
   python -m venv bioview_env
   bioview_env\Scripts\activate
   ```
6. **安裝 BioView(本 repo 的修改版)**
   ```bat
   pip install git+https://github.com/Jessiemd11/Bioview.git
   ```
   會自動安裝 numpy(<2)、PyQt6、pyqtgraph、scipy、h5py、matplotlib、qtawesome、pygame、darkdetect 與 Python 版 `uhd`。
   若程式在其他分支,在網址後加 `@分支名`。

   > 單純 `pip install bioview` 只會裝到 PyPI 上**未修改**的版本(沒有校正功能與上述 annotation 修改)。
7. **確認安裝**
   ```bat
   python -c "import uhd, bioview; print('ok')"
   ```
8. **執行**
   取得 `launch_bioview.py`(`git clone` 本 repo,或直接下載該檔),依需求修改參數後:
   ```bat
   bioview_env\Scripts\activate
   python launch_bioview.py
   ```
   - `save_dir`:預設為使用者的 `Downloads` 資料夾,可改成其他路徑。
   - `if_freq`、`rx_gain`、`tx_gain`、`samp_rate` 等為 USRP 參數,依實驗調整。
   - 程式會在目前資料夾寫入 `crash.log`(faulthandler)。

### 常見問題

- `import uhd` 失敗:確認已安裝 UHD 4.8.0.0 並**重開終端機**;虛擬環境內要有 `uhd` 套件(`pip show uhd`)。
- `uhd_find_devices` 找得到裝置,但 Bioview 一直連不上:`device_name`(如 `'MyB210'`)只是顯示用的標籤,
  真正拿來搜尋裝置的是 `device_args`(第一次連線、還沒快取序號時才會用到)。若 `device_args` 不是合法的
  UHD 查詢字串(例如空字串以外、又不是 `type=`/`serial=`/`addr=` 開頭),搜尋不到裝置就會連線失敗。
  單一 B210 可設 `device_args = 'type=b200'`;若同時接多台 USRP,請用 `uhd_usrp_probe` 輸出中
  `_____MBOARD serial` 欄位的序號,設成 `device_args = 'serial=XXXXXXXX'`。
  第一次連線成功後,序號會被寫進 `%USERPROFILE%\.bioview\serial_maps`(以 `device_name` 為 key);
  之後連線都會直接用快取的序號,不會再用到 `device_args`。若序號快取有誤(例如換了裝置卻用同一個
  `device_name`),可以刪除該檔案讓程式重新搜尋。
- 出現 `WARNING: Version conflict between uhd.dll(...) and the Python API build version(...)`:代表系統安裝的 UHD 驅動版本與虛擬環境內 Python `uhd` 套件版本不一致(例如 pip 裝到比驅動新的版本)。
  在虛擬環境內執行 `pip install uhd==<驅動版本>`(例如 `pip install uhd==4.8.0.0`)讓兩者版本一致即可;`pyproject.toml` 已將 `uhd` 鎖定在 4.8.0.0,正常安裝不會再發生。
- `pip install` 途中出現 `THESE PACKAGES DO NOT MATCH THE HASHES ...`:通常是網路不穩(校園網路/VPN/防毒攔截)或本機 pip 快取損毀所致,而非套件被竄改。
  先清快取再重裝:`pip cache purge` 後 `pip install --no-cache-dir git+https://github.com/Jessiemd11/Bioview.git`。若同一個套件、同樣的 hash 仍反覆失敗,才需要懷疑網路環境（如公司代理伺服器的 SSL 攔截）。
- 偵測不到 B210:重跑 Zadig 安裝 WinUSB、換 USB 3.0 埠/線、再執行 `uhd_find_devices`。
- 舊的批次檔(例如 `launch bioview.bat`)內是寫死的絕對路徑,換電腦時需改成新電腦上的虛擬環境與腳本路徑。
- BIOPAC 整合需自行取得 BIOPAC Hardware API,本 repo 不包含。
- Log 出現 `Tx underflows` / `Tx restarted` / `Rx dropped ... samples` / `Save pipeline is X s behind`:代表電腦來不及處理 B210 的資料串流(Tx 斷訊或 Rx 掉樣本)。
  程式會讓相位在斷點後自動對齊,但仍會留下短暫空白。錄製時建議:筆電接上電源、電源計畫選「高效能」、
  關閉 USB 選擇性暫停與睡眠、B210 直接插 USB 3.0 埠、錄製中避免切換到其他吃資源的程式(影片、雲端同步、大量複製)、不要在錄製中調整增益。
  程式啟動時會自動關閉 Windows 對 Bioview 的節能節流(EcoQoS)並將優先權設為「高」,結果會顯示在 Log。
- 每次開啟存檔的錄製,Log 會同步存成 `<檔名>_log.txt`,與 `.h5` 放在同一資料夾。

## 授權

GPL-v3,見 [LICENSE](LICENSE)。原始著作權屬上游 BioView 作者 Aakash Kapoor;本 repo 為其修改版。
