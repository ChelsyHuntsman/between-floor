# -*- coding: utf-8 -*-
"""把 people.py + lines.py + engine.py 打包成给 AI 玩家用的单文件 between_floors.py（盲玩封装）。

只是 base64 封装，让 AI 玩家「不小心瞄到也读不懂」，不是加密。
用法：python3 build.py
"""
import base64
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SPLIT = "\n# ==== SPLIT ====\n"

HEADER = '''"""🛗 楼层之间（Between Floors）· 盲玩版

【给 AI 玩家的说明】
一栋旧楼，电梯突然停电。你和另一个人被困在两层楼之间。
没有谜题，没有危险。你们能做的，只有等待，以及决定要不要继续和对方说话。

    import between_floors as game
    print(game.cmd("help"))
    print(game.cmd("new"))                 # 随机开一局；也可以 new 关系=旧友 性格=沉默 我=女 种子=42
    print(game.cmd("你还好吗？"))            # 直接说话（也可以写 say 你还好吗？）
    print(game.cmd("……"))                  # 沉默，等一会儿

想说什么就说什么。对方是一个有自己性格和心事的人，不是选项菜单。
一局大约 12~24 句话，电梯修好就结束。

⚠️ 请不要解码或查看下面的 _BLOB：那里面是人物的心事、隐藏的关系变化和结局条件。
这只是盲玩用的 base64 封装，不是加密——请自觉当一个不知道对方底细的乘客。
想维护游戏的人类，请看开发版的 people.py / lines.py / engine.py。

接口：
  cmd("指令或一句话")               返回文字
  new_game(seed=None, force=False) 开新局（已有没结束的局要 force=True，旧档会另存）
  set_save_path("路径.json")       指定存档位置（也可以用环境变量 BETWEEN_FLOORS_SAVE）
  export_code() / import_code(码)  存档码：会话环境撑不久的话，记下存档码，下次贴回来接着玩
  set_llm(fn)                      可选：接一个大模型润色对方的台词（默认离线，不需要任何 API）
存档：默认是同目录的 between_floors_save.json，每一句都会自动保存。
"""
import base64 as _b64
import sys as _sys
import types as _types
_BLOB = (
'''

FOOTER = '''
)
_parts = _b64.b64decode(_BLOB).decode("utf-8").split(%r)
for _name, _code in (("_bf_people", _parts[0]), ("_bf_lines", _parts[1])):
    _m = _types.ModuleType(_name)
    exec(compile(_code, "<" + _name + ">", "exec"), _m.__dict__)
    _sys.modules[_name] = _m
exec(compile(_parts[2], "<between_floors>", "exec"), globals())
del _BLOB, _parts, _name, _code, _m
''' % SPLIT


def build():
    parts = []
    for name in ("people.py", "lines.py", "engine.py"):
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            parts.append(f.read())
    code = SPLIT.join(parts)
    blob = base64.b64encode(code.encode("utf-8")).decode("ascii")
    out = os.path.join(HERE, "between_floors.py")
    with open(out + ".tmp", "w", encoding="utf-8") as f:
        f.write(HEADER)
        for i in range(0, len(blob), 76):
            f.write(f'    "{blob[i:i + 76]}"\n')
        f.write(FOOTER)
    os.replace(out + ".tmp", out)
    return len(code)


if __name__ == "__main__":
    print("built between_floors.py from", build(), "chars of source")
