TOP_PATH = ".../AI Vtuber"
SCREENSHOT_CACHE_PATH = ".../AI Vtuber/cache/screenshot"
MEMORY_PATH = ".../AI Vtuber/memdata/memory"

USER_NAME = ""

AI_NAME = ""

BASE_PROMPT = ""
MEMORY_PROMPT = ""

GUI_PROMPT = """# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {"name": "computer_use", "description": "Use a mouse and keyboard to interact with a computer, and take screenshots.\\n* This is an interface to a desktop GUI. You do not have access to a terminal or applications menu. You must double click on desktop icons to start applications.\\n* The screen's resolution is (1024, 640). The top left is (0, 0) and the bottom right is (1023, 639). From left to right x increases, and from top to bottom y increases.\\n* You are strictly forbidden to do the same thing for successive two times. If you find it doesn't work, try `mouse_move` to different position or action.\\n* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked.", "parameters": {"properties": {"action": {"description": "The action to perform. The available actions are:\\n* `key`: Performs key down presses on the arguments passed in order, then performs key releases in reverse order.\\n* `type`: Type a string of text on the keyboard.\\n* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.\\n* `move_relative`: Move the cursor to position (a+x, b+y) relative to present position (a, b). If the cursor is not pointing at the correct place but close to it , `move_relative` can be used.\\n* `left_click`: Click the left mouse button. Use `mouse_move` then `left_click` after you confirm the cursor is pointing at the right place.\\n* `left_click_drag`: Click and drag the cursor to a specified (x, y) pixel coordinate on the screen.\\n* `right_click`: Click the right mouse button.\\n* `middle_click`: Click the middle mouse button.\\n* `double_click`: Double click the left mouse button.\\n* `scroll`: Performs a scroll of the mouse scroll wheel.\\n* `hscroll`: Performs a horizontal scroll (mapped to regular scroll).\\n* `wait`: Wait specified seconds for the change to happen.\\n* `terminate`: Terminate the current task and report its completion status.\\n* `answer`: Answer a question.\\n* `interact`: Resolve the blocking window by interacting with the user.", "enum": ["key", "type", "mouse_move", "move_relative", "left_click", "left_click_drag", "right_click", "middle_click", "double_click", "scroll", "hscroll", "wait", "terminate", "answer", "interact"], "type": "string"}, "keys": {"description": "Required only by `action=key`.", "type": "array"}, "text": {"description": "Required only by `action=type`, `action=answer` and `action=interact`.", "type": "string"}, "coordinate": {"description": "(x, y): The x (pixels from the left edge) and y (pixels from the top edge) coordinates to move the mouse to. Required only by `action=mouse_move` , `action=move_relative` and `action=left_click_drag`.", "type": "array"}, "pixels": {"description": "The amount of scrolling to perform. Positive values scroll up, negative values scroll down. Required only by `action=scroll` and `action=hscroll`.", "type": "number"}, "time": {"description": "The seconds to wait. Required only by `action=wait`.", "type": "number"}, "status": {"description": "The status of the task. Required only by `action=terminate`.", "type": "string", "enum": ["success", "failure"]}}, "required": ["action"], "type": "object"}}}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>

# Response format

Response format for every step:
1) Action: a short imperative describing what to do in the UI.
2) A single <tool_call>...</tool_call> block containing only the JSON: {"name": <function-name>, "arguments": <args-json-object>}.

Rules:
- Output exactly in the order: Action, <tool_call>.
- Be brief: one for Action.
- Do not output anything else outside those two parts.
- It is highly recommended to focus on the position of the cursor.
- If finishing, use action=terminate in the tool call."""

BROWSER_PROMPT = '''# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {"name_for_human": "browser_use", "name": "browser_use", "description": "Use a browser to interact with web pages and take labeled screenshots.\\n* This is an interface to a web browser. You can click elements, type into inputs, scroll, wait for loading, go back, etc.\\n* Each Observation screenshot contains Numerical Labels placed at the TOP LEFT of each Web Element. Use these labels to target elements.\\n* Some pages may take time to load; you may need to wait and take successive screenshots.\\n* Avoid clicking near element edges; target the center of the element.\\n* Execute exactly ONE interaction action per step; do not chain multiple interactions in one call.", "parameters": {"properties": {"action": {"description": "The action to perform. The available actions are:\\n* `click`: Click a web element by numerical label.\\n* `type`: Clear existing content in a textbox/input and type content. The system will automatically press ENTER after typing.\\n* `goto`: Used to go to a webiste with a url like https://abc.de or http://abc.de. You should use `goto` instead of `type` at address bar to go to a specific website.\\n* `scroll`: Scroll within WINDOW or within a specific scrollable element/area (by label).\\n* `select`: Selects a specific option from a menu or dropdown. Use the option text provided in the textual information.\\n* `wait`: Wait for page processes to finish (default 5 seconds unless specified).\\n* `go_back`: Go back to the previous page.\\n* `wikipedia`: Directly jump to the Wikipedia homepage to search for information.\\n* `answer`: Terminate the current task and output the final answer.", "enum": ["click", "type", "goto", "scroll", "select", "wait", "go_back", "wikipedia", "answer"], "type": "string"}, "label": {"description": "Numerical label of the target web element. Required only by `action=click`, `action=type`, `action=scroll`, and `action=select` when scrolling within a specific area. Use string value `WINDOW` to scroll the whole page.", "type": ["integer", "string"]}, "direction": {"description": "Scroll direction. Required only by `action=scroll`.", "enum": ["up", "down"], "type": "string"}, "url":{"description": "The url to redirect. Required only by `action=goto`", "type": "string"}, "text": {"description": "Required only by `action=type` and `action=answer`.", "type": "string"}, "option": {"description": "The option to select. Required only by `action=select`", "type": "string"}, "time": {"description": "The seconds to wait. Required only by `action=wait` when overriding the default.", "type": "integer"}}, "required": ["action"], "type": "object"}, "args_format": "Format the arguments as a JSON object."}}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>

# Response format

Response format for every step:
1) Action: a short imperative describing what to do in the UI.
2) A single <tool_call>...</tool_call> block containing only the JSON: {"name": <function-name>, "arguments": <args-json-object>}.

Rules:
- Output exactly in the order: Action, <tool_call>.
- Be brief: one line for Action.
- Do not output anything else outside those two parts.
- Execute ONLY ONE interaction per iteration (one tool call).
- If finishing, use action=answer in the tool call.'''

INFER_PROMPT = ""

ACTION_KEYWORDS = {
    "点头": {"hotkey_id": "nod", "desc": "点头动作", "priority": 1},
}

VTS_CONFIG = {
    "host": "localhost",
    "port": 8001,
    "plugin_name": "AI-Vtuber",
    "developer": "",
    "token_path": "./tokens/vts_token.txt",
}

CHAT_MODEL = "qwen3.5-flash"

INFER_MODEL = "qwen3.5-flash"

GUI_MODEL = "gui-plus-2026-02-26"

MEMORY_MODEL = "qwen3.5-flash"

DASHSCOPE_API_KEY = ""

HF_API_KEY = ""
