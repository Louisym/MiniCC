import json     
import subprocess                                                                                                                                  
from typing import Callable

class ToolRegistry:
    def __init__(self):
        self._handlers: dict[str, Callable] = {}
    def register(self, name:str, handler:Callable):
        if name in self._handlers:
            return f'The tool: {name} has been registered'
            
        self._handlers[name] = handler
        return self
    def execute(self, name:str, tool_input_json:str):
        if name not in self._handlers:
            return f'tool:{name} has not been registered yet'
        tool = self._handlers.get(name, None)
        return tool(tool_input_json)

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