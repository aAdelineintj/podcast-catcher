import whisper
import json

def transcribe_vibe_check(audio_path):
    # 1. 加载模型 
    # M1 Air 建议用 'base'：速度快，识别准。
    # 如果想更准，可以换 'small'，但速度会慢一点。
    print("正在加载 Whisper 模型 (base)...")
    model = whisper.load_model("base")

    # 2. 开始转录
    # task="transcribe" 是转录原文
    # 如果你想直接把法语翻译成英语，可以设置 task="translate"
    print(f"正在分析音频: {audio_path}")
    result = model.transcribe(audio_path, verbose=False)

    # 3. 提取我们需要的数据
    transcript_data = []
    for segment in result['segments']:
        item = {
            "start": round(segment['start'], 2),
            "end": round(segment['end'], 2),
            "text": segment['text'].strip()
        }
        transcript_data.append(item)
        # 实时打印到控制台，让你有 Vibe 感
        print(f"[{item['start']}s -> {item['end']}s]: {item['text']}")

    # 4. 保存为 JSON，给未来的前端用
    with open("podcast_result.json", "w", encoding="utf-8") as f:
        json.dump(transcript_data, f, ensure_ascii=False, indent=2)

    print("\n✅ 完成！数据已存入 podcast_result.json")

if __name__ == "__main__":
    # 确保你文件夹里有一个名为 006.mp3 的文件
    # 或者把这里改成你本地音频的路径
    
    transcribe_vibe_check("/Users/mac/Documents/my_project/006.mp3")
   
    print("错误：没找到 006.mp3 文件。请先放一个音频文件到文件夹里。")