curl -X POST 'https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization' \
  -H "Authorization: Bearer ${api_key}" \
  -H 'Content-Type: application/json; charset=utf-8' \
  -d '{
    "model": "qwen-voice-design",
    "input": {
        "action": "list",
        "page_size": 10,
        "page_index": 0
    }
}'