import json
import time
import hashlib
import random
import requests
import os
from dotenv import load_dotenv

load_dotenv()

# --- 这里填入你的百度翻译开发者信息 ---
APP_ID = os.getenv("BAIDU_APP_ID")
SECRET_KEY = os.getenv("BAIDU_SECRET_KEY")

def baidu_translate(text, from_lang='fra', to_lang='zh'):
    """
    fra: 法语, zh: 中文 (百度 API 语种代码)
    """
    url = "http://api.fanyi.baidu.com/api/trans/vip/translate"
    salt = str(random.randint(32768, 65536))
    
    # 按照百度文档要求的 MD5 签名逻辑
    sign_str = APP_ID + text + salt + SECRET_KEY
    sign = hashlib.md5(sign_str.encode('utf-8')).hexdigest()
    
    params = {
        'q': text,
        'from': from_lang,
        'to': to_lang,
        'appid': APP_ID,
        'salt': salt,
        'sign': sign
    }
    
    try:
        # 使用 http 而非 https 可以避开你电脑烦人的 SSL 证书问题
        r = requests.get(url, params=params, timeout=10)
        result = r.json()
        if 'trans_result' in result:
            return result['trans_result'][0]['dst']
        else:
            print(f"API 报错: {result}")
            return "翻译失败"
    except Exception as e:
        print(f"网络异常: {e}")
        return "网络异常"

def start_job():
    # 读取数据
    with open('podcast_result.json', 'r', encoding='utf-8') as f:
        segments = json.load(f)

    # 验证是否为列表结构
    if not isinstance(segments, list):
        print("错误：JSON 格式不是预期的列表结构，请检查数据。")
        return

    print(f"🚀 格式校验成功！检测到 {len(segments)} 段音频文案，开始翻译...")

    for i, seg in enumerate(segments):
        # 提取原文
        original = seg.get('text', '').strip()
        
        if original:
            # 调用翻译函数
            translated = baidu_translate(original, from_lang='fra', to_lang='zh')
            seg['trans'] = translated
            print(f"进度: [{i+1}/{len(segments)}] -> {translated[:20]}...")
        else:
            seg['trans'] = ""

        # 百度免费版 QPS=1，必须等待
        time.sleep(1.2)

    # 保存最终带翻译的结果
    with open('podcast_with_trans.json', 'w', encoding='utf-8') as f:
        json.dump(segments, f, ensure_ascii=False, indent=4)
    
    print("\n✅ 阶段 2 完美收工！已生成 podcast_with_trans.json")



if __name__ == "__main__":
    start_job()
