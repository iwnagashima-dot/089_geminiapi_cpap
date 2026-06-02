import psycopg2
import shutil
import os
import fitz
import oracledb
import datetime
import socket
import time
from PIL import Image
import pytesseract
import sys
from pathlib import Path
import configparser
from google import genai
from google.genai import types

# =========================
# EXE対応 基本フォルダ
# =========================
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

DIST_DIR = BASE_DIR / "dist"
DIST_DIR.mkdir(exist_ok=True)

TEST_DIR = DIST_DIR / "test"
TEST_DIR.mkdir(exist_ok=True)

# =========================
# Oracle Client
# =========================
oracledb.init_oracle_client(
    lib_dir=str(BASE_DIR / "instantclient_23_4")
)

# =========================
# Tesseract
# =========================
pytesseract.pytesseract.tesseract_cmd = str(
    BASE_DIR / "Tesseract-OCR" / "tesseract.exe"
)

# =========================
# API設定読み込み
# =========================
config = configparser.ConfigParser()
read_files = config.read(BASE_DIR / "api.ini", encoding="utf-8")

print("BASE_DIR:", BASE_DIR)
print("api.ini:", BASE_DIR / "api.ini")
print("read_files:", read_files)
print("sections:", config.sections())

API_KEY = config["gemini"]["API_KEY"].strip()

# =========================
# Gemini新SDK設定
# =========================
client = genai.Client(api_key=API_KEY)

MODEL_NAME = "gemini-2.5-flash"
print(f"使用モデル: {MODEL_NAME}")


# =========================
# プロンプト読み込み
# =========================
def load_prompt(text):
    lower_text = text.lower()

    if "asv" in lower_text:
        print("ASV検出 → prompt_asv.txt 使用")
        prompt_file = BASE_DIR / "prompt_asv.txt"
    else:
        print("通常プロンプト使用")
        prompt_file = BASE_DIR / "prompt.txt"

    with open(prompt_file, "r", encoding="utf-8") as f:
        return f.read()


# =========================
# Gemini処理
# =========================
def run_gemini(api_request_text):
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=api_request_text,
        config=types.GenerateContentConfig(
            temperature=0.2
        )
    )

    if response.text:
        return response.text

    return ""


# =========================
# メインループ
# =========================
print("システム稼働開始...")

while True:
    conn = None
    cur = None

    try:
        # =========================
        # PostgreSQL接続
        # =========================
        conn = psycopg2.connect(
            dbname="gazouDB",
            user="postgres",
            password="medicalin",
            host="192.168.122.54",
            port="5432"
        )

        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM gazou_tbl
            WHERE sindan4 <> '(済)'
              AND kensamei = 'CPAP'
              AND date >= '2024/09/27'
            ORDER BY date
        """)

        rows = cur.fetchall()

        # =========================
        # 行処理
        # =========================
        for index, row in enumerate(rows, start=1):
            print("=" * 40)
            print(f"{'進捗':<10}: {index}/{len(rows)}")
            print(f"{'患者ID':<10}: {row[3]}")
            print(f"{'検査日':<10}: {row[6]}")
            print(f"{'処理開始':<10}: {time.strftime('%H:%M:%S')}")
            print("=" * 40)

            try:
                renban = row[0]

                safe_date = str(row[6]).replace("/", "")

                pdf_path = TEST_DIR / f"{row[3]}-{safe_date}.pdf"
                jpg_path = TEST_DIR / f"{row[3]}-{safe_date}.jpg"

                src_pdf = os.path.join(row[14], "001.pdf")
                src_jpg = os.path.join(row[14], "001.jpg")

                text = ""

                # =========================
                # PDF処理
                # =========================
                if os.path.exists(src_pdf):
                    if pdf_path.exists():
                        pdf_path.unlink()

                    shutil.copy(src_pdf, str(pdf_path))
                    print(f"PDFコピー: {src_pdf}")

                    doc = None

                    try:
                        doc = fitz.open(str(pdf_path))

                        if row[2] == "ペースメーカ":
                            pages_to_extract = [1, 2]
                        elif row[2] == "ホルター結果":
                            pages_to_extract = [1]
                        elif row[2] == "CPAP":
                            pages_to_extract = list(range(6))
                        else:
                            pages_to_extract = [0]

                        total_pages = len(doc)

                        for page_num in pages_to_extract:
                            if page_num < total_pages:
                                page = doc[page_num]
                                text += page.get_text()

                                if len(text) >= 18000:
                                    text = text[:18000]
                                    break

                        doc.close()

                    except Exception as e:
                        print(f"PDF処理エラー: {e}")

                        try:
                            if doc:
                                doc.close()
                        except:
                            pass

                        continue

                # =========================
                # JPG OCR処理
                # =========================
                elif os.path.exists(src_jpg):
                    if jpg_path.exists():
                        jpg_path.unlink()

                    shutil.copy(src_jpg, str(jpg_path))
                    print(f"JPGコピー: {src_jpg}")

                    try:
                        image = Image.open(str(jpg_path))

                        custom_config = r"--oem 3 --psm 6"

                        text = pytesseract.image_to_string(
                            image,
                            config=custom_config,
                            lang="jpn"
                        )

                    except Exception as e:
                        print(f"OCRエラー: {e}")
                        continue

                else:
                    print(f"renban={renban}: 元ファイルなし")
                    continue

                # =========================
                # Gemini
                # =========================
                PROMPT_TEXT = load_prompt(text)
                api_request_text = text + "\n\n" + PROMPT_TEXT

                try:
                    result_text = run_gemini(api_request_text)

                except Exception as e:
                    print(f"Gemini API エラー: {e}")
                    continue

                # =========================
                # ログ保存
                # =========================
                try:
                    log_file = DIST_DIR / "log.txt"

                    with open(log_file, "a", encoding="utf-8") as file:
                        file.write(f"{row[3]} {row[2]} {row[6]}\n\n")
                        file.write(result_text + "\n\n\n\n\n")

                except Exception as e:
                    print(f"ログ保存エラー: {e}")

                # =========================
                # Oracle登録
                # =========================
                try:
                    dsn_tns = oracledb.makedsn(
                        "192.168.122.3",
                        "1521",
                        service_name="iwamoto"
                    )

                    connection = oracledb.connect(
                        user="iwamoto",
                        password="d83o9i",
                        dsn=dsn_tns
                    )

                    cursor = connection.cursor()

                    strDate = row[6]
                    qryNo = row[3]

                    lower_text = text.lower()

                    kensamei = row[2]

                    if "asv" in lower_text:
                        print("ASV検出 → 検査名変更")
                        kensamei = "ASV"

                    qryMemo = (
                        row[3] + " " +
                        kensamei + " " +
                        row[6] + "<br>" +
                        result_text[:3000].replace("\n", "<br>")
                    )

                    strNow = datetime.datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                    strIPAdd = socket.gethostbyname(
                        socket.gethostname()
                    )

                    strSQL = """
                    INSERT INTO T_処方変更台帳 (
                        RECNO,
                        "日付",
                        "患者NO",
                        "内容",
                        "作成日",
                        "更新日",
                        "更新者",
                        "確認"
                    ) VALUES (
                        T_処方変更台帳RECNO.NEXTVAL,
                        TO_DATE(:1, 'YYYY-MM-DD'),
                        :2,
                        :3,
                        TO_DATE(:4, 'YYYY-MM-DD HH24:MI:SS'),
                        TO_DATE(:5, 'YYYY-MM-DD HH24:MI:SS'),
                        :6,
                        0
                    )
                    """

                    cursor.execute(
                        strSQL,
                        (
                            strDate,
                            qryNo,
                            qryMemo,
                            strNow,
                            strNow,
                            strIPAdd
                        )
                    )

                    connection.commit()

                    cursor.close()
                    connection.close()

                except Exception as e:
                    print(f"Oracle登録エラー: {e}")

                # =========================
                # PostgreSQL更新
                # =========================
                try:
                    cur.execute(
                        """
                        UPDATE gazou_tbl
                        SET sindan4 = %s
                        WHERE renban = %s
                        """,
                        ("(済)", renban)
                    )

                    conn.commit()

                    print(f"renban={renban}: '(済)' に更新完了")

                except Exception as e:
                    print(f"Postgres UPDATEエラー: {e}")

            except Exception as row_error:
                print(f"renban={row[0]} 行処理エラー: {row_error}")
                continue

        cur.close()
        conn.close()

        time.sleep(10)

    except Exception as e:
        print(f"メインループエラー: {e}")

        try:
            if cur:
                cur.close()
        except:
            pass

        try:
            if conn:
                conn.close()
        except:
            pass

        time.sleep(60)
