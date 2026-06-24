"""組み込みユースケース(UC-02)。テンプレート=データの実例。

DBには置かずコード同梱(specs/08)。idは 'builtin-' プレフィックスで固定。
"""

BUILTIN_USECASES: list[dict] = [
    {
        "id": "builtin-summarize",
        "name": "要約",
        "description": "文章を指定の長さで要約します",
        "icon": "document",
        "tags": ["文章"],
        "builtin": True,
        "visibility": "public",
        "fields": [
            {
                "name": "text",
                "label": "本文",
                "type": "textarea",
                "required": True,
                "placeholder": "要約したい文章を貼り付けてください",
            },
            {
                "name": "length",
                "label": "長さ",
                "type": "select",
                "options": ["1行", "3行", "1段落", "詳細（箇条書き）"],
                "default": "3行",
            },
        ],
        "template": "次の文章を{{length}}で要約してください。"
        "要約のみを出力してください。\n\n---\n{{text}}",
    },
    {
        "id": "builtin-writing",
        "name": "執筆・校閲",
        "description": "文章の校正・リライト・トーン調整を行います",
        "icon": "edit",
        "tags": ["文章"],
        "builtin": True,
        "visibility": "public",
        "fields": [
            {
                "name": "text",
                "label": "本文",
                "type": "textarea",
                "required": True,
                "placeholder": "対象の文章",
            },
            {
                "name": "mode",
                "label": "指示",
                "type": "select",
                "options": [
                    "誤字脱字・文法の校正",
                    "より自然な文章へのリライト",
                    "ビジネス敬語への変換",
                    "英文の校正（ネイティブ表現）",
                ],
                "default": "誤字脱字・文法の校正",
            },
            {
                "name": "note",
                "label": "補足指示（任意）",
                "type": "text",
                "required": False,
                "placeholder": "例: 箇条書きは維持して",
            },
        ],
        "template": "次の文章に対して「{{mode}}」を行ってください。{{note}}\n"
        "変更箇所がわかるように、結果の後に主な変更点を箇条書きで示してください。"
        "\n\n---\n{{text}}",
    },
    {
        "id": "builtin-translate",
        "name": "翻訳",
        "description": "テキストを指定言語に翻訳します",
        "icon": "translate",
        "tags": ["文章"],
        "builtin": True,
        "visibility": "public",
        "fields": [
            {
                "name": "text",
                "label": "本文",
                "type": "textarea",
                "required": True,
                "placeholder": "翻訳したいテキスト",
            },
            {
                "name": "lang",
                "label": "翻訳先",
                "type": "select",
                "options": ["日本語", "英語", "中国語（簡体字）", "韓国語", "フランス語"],
                "default": "英語",
            },
            {
                "name": "tone",
                "label": "トーン",
                "type": "select",
                "options": ["標準", "ビジネス", "カジュアル", "技術文書"],
                "default": "標準",
            },
        ],
        "template": "次のテキストを{{lang}}に翻訳してください（トーン: {{tone}}）。"
        "翻訳のみを出力してください。\n\n---\n{{text}}",
    },
    {
        "id": "builtin-web-extract",
        "name": "Webコンテンツ抽出",
        "description": "URLの本文を取得して要約・抽出します",
        "icon": "link",
        "tags": ["Web"],
        "builtin": True,
        "visibility": "public",
        "fields": [
            {
                "name": "page",
                "label": "URL",
                "type": "url",
                "required": True,
                "placeholder": "https://example.com/article",
            },
            {
                "name": "request",
                "label": "やりたいこと",
                "type": "select",
                "options": [
                    "3行で要約",
                    "重要ポイントを箇条書き",
                    "事実関係だけを抽出",
                    "日本語で要約（原文が外国語の場合）",
                ],
                "default": "3行で要約",
            },
        ],
        "template": "次のWebページ本文に対して「{{request}}」を行ってください。\n\n---\n{{page}}",
    },
    {
        "id": "builtin-diagram",
        "name": "ダイアグラム生成",
        "description": "説明からmermaid図を生成します",
        "icon": "diagram",
        "tags": ["図解"],
        "builtin": True,
        "visibility": "public",
        "fields": [
            {
                "name": "content",
                "label": "図にしたい内容",
                "type": "textarea",
                "required": True,
                "placeholder": "例: ユーザー登録からログインまでの流れ",
            },
            {
                "name": "kind",
                "label": "図の種類",
                "type": "select",
                "options": [
                    "フローチャート",
                    "シーケンス図",
                    "状態遷移図",
                    "ER図",
                    "ガントチャート",
                ],
                "default": "フローチャート",
            },
        ],
        "template": "次の内容を表すmermaidの{{kind}}を生成してください。\n"
        "注意: ノードラベルに括弧や特殊文字を含める場合は必ずダブルクオートで囲み、"
        "正しいmermaid構文で出力してください。図の前に1行で概要説明を付けてください。"
        "\n\n---\n{{content}}",
    },
]
