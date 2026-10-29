"""Small validated messages for the existing player pipe; no GUI or engine."""
import json


def decode_command(line):
    if len(line) > 2048: raise ValueError('播放器命令过长。')
    if line.strip() == 'CLOSE': return {'command':'close'}
    value = json.loads(line)
    if not isinstance(value,dict): raise ValueError('播放器命令必须是对象。')
    command=value.get('command')
    if command in ('toggle','visibility','close'):
        if set(value)!={'command'}: raise ValueError('播放器命令字段无效。')
    elif command == 'volume':
        if set(value)!={'command','value'} or type(value['value']) is not int or not 0<=value['value']<=100:
            raise ValueError('音量必须为 0–100。')
    elif command == 'seek':
        if set(value)!={'command','position_ms'} or type(value['position_ms']) is not int or not 0<=value['position_ms']<=2**53:
            raise ValueError('跳转位置无效。')
    elif command == 'relative':
        if set(value)!={'command','seconds'} or value['seconds'] not in (-5,5) or type(value['seconds']) is not int:
            raise ValueError('只支持前后 5 秒。')
    else: raise ValueError('未知播放器命令。')
    return value


def encode_command(command, **fields):
    value=decode_command(json.dumps({'command':command,**fields},ensure_ascii=False))
    return 'CLOSE\n' if command=='close' else json.dumps(value,ensure_ascii=False)+'\n'
