import json     
import subprocess                                                                                                                                  
from typing import Callable

class ToolRegistry:
    def __init__(self):
        self._handlers: dict[str, Callable] = {}

    def register(self, name: str, handler: Callable) -> "ToolRegistry":
        if name in self._handlers:
            raise ValueError(f"Tool already registered: {name}")
        self._handlers[name] = handler
        return self

    def execute(self, name: str, tool_input_json: str) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"Unknown tool: {name}")
        return handler(tool_input_json)

def bash_tool(input_json:str):
    data = json.loads(input_json)
    cmd = data['command']
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += f'\nSTDERR: {result.stderr}'
        return output
    except subprocess.TimeoutExpired:
        return 'ERROR: timeout for 30s'
    
def read_tool(input_json:str):
    data = json.loads(input_json)
    path = data.get('path', '')
    try:
        with open(path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        return f'ERROR: file not found {path}'
    return content

def write_tool(input_json:str):
    data = json.loads(input_json)
    path = data.get('path', '')
    content = data.get('content', '')
    try:
        with open(path, 'w') as f:
            f.write(content)
    except FileNotFoundError:
        return f'ERROR: directory not found {path}'
    return f'OK: wrote to {path}'