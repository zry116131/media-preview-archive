#!/usr/bin/env python3
"""Send one local image to the JoyAI OpenAI-compatible endpoint."""

import argparse
import base64
import json
import mimetypes
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--endpoint", default="http://127.0.0.1:7060/v1")
    parser.add_argument("--model", default="JoyAI-VL-Interaction-Preview")
    args = parser.parse_args()

    mime_type = mimetypes.guess_type(args.image)[0] or "image/jpeg"
    with open(args.image, "rb") as image_file:
        image_data = base64.b64encode(image_file.read()).decode("ascii")

    payload = {
        "model": args.model,
        "temperature": 0.1,
        "max_tokens": 300,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "请用中文识别这张拍摄素材画面。简洁描述主体、场景、动作、"
                            "镜头类型，并给出5个便于检索的关键词。"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_data}"
                        },
                    },
                ],
            }
        ],
    }
    request = urllib.request.Request(
        f"{args.endpoint.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.load(response)
    print(result["choices"][0]["message"]["content"])


if __name__ == "__main__":
    main()
