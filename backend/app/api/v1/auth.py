






curl -X 'POST' \
'http://0.0.0.0:8000/v1/chat/completions' \
-H 'accept: application/json' \
-H 'Content-Type: application/json' \
-d '{
    "model": "deepseek-ai/deepseek-v4.1-flash",
    "messages": [{"role":"user", "content":"Hello! How are you?"}],
    "max_tokens": 64
}'

