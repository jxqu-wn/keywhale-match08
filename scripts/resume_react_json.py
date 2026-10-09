"""Resume exhausted ReAct records after conservative end-of-JSON closure repair.

Only missing final delimiters are appended. Values, fields, operators and SQL
remain unchanged. Recovered candidates are checked by the existing tools and
the model must subsequently finish the query. Original records remain archived.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.context import Context
from text2query.shared.curl_batch import build
from text2query.shared.generation import completion_payload, parse_output, write_offline_request
from text2query.react.tools import QueryTools
from text2query.react.pipeline import summarize


def close_json(content):
    try:
        return parse_output(content), ''
    except ValueError:
        pass
    text = content.strip()
    stack, quoted, escaped = [], False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in '{[':
            stack.append(character)
        elif character in '}]':
            expected = '{' if character == '}' else '['
            if not stack or stack.pop() != expected:
                raise ValueError('JSON内部括号不匹配，不能仅补结尾修复')
    if quoted or not stack or len(stack) > 5:
        raise ValueError('无法安全仅补JSON末尾括号')
    suffix = ''.join('}' if character == '{' else ']' for character in reversed(stack))
    return parse_output(text + suffix), suffix


def evidence_tools(context, record):
    tools = QueryTools(context, record['context'])
    for turn in record['trace']:
        if turn['action'] in ('get_schema', 'search_knowledge', 'search_examples') and turn['observation'].get('ok'):
            tools.dispatch(turn['action'], turn['args'])
    return tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--auth-header-file', type=Path)
    args = parser.parse_args()
    context = Context(False)
    config = json.loads((args.run / 'config.json').read_text())
    archive = args.run / 'before_json_recovery'
    ids = []
    for row in json.loads((args.run / 'sample.json').read_text()):
        path = args.run / (str(row['question_id']) + '.json')
        record = json.loads(path.read_text())
        if record.get('prediction') and not record.get('errors'):
            continue
        if record.get('errors') != ['step_budget_exhausted']:
            continue
        ids.append(row['question_id'])
        tools = evidence_tools(context, record)
        state_path = args.run / (str(row['question_id']) + '.json_recovery.json')
        if not state_path.exists():
            archive.mkdir(exist_ok=True)
            (archive / path.name).write_bytes(path.read_bytes())
            envelope, suffix = close_json(record['attempts'][-1]['content'])
            if envelope.get('action') not in ('check_query', 'finish'):
                raise ValueError('结尾修复不是查询检查/提交动作')
            checked = tools.dispatch('check_query', envelope['args'])
            if not checked.get('ok'):
                raise ValueError('结尾修复候选未通过现有校验：' + json.dumps(checked, ensure_ascii=False))
            initial = json.loads((args.run / (str(row['question_id']) + '.request.0.json')).read_text())
            messages = initial['messages'][:]
            for turn in record['trace']:
                if turn['action'] in ('get_schema', 'search_knowledge', 'search_examples') and turn['observation'].get('ok'):
                    messages += [{'role': 'assistant', 'content': record['attempts'][turn['step']]['content']},
                                 {'role': 'user', 'content': json.dumps({'observation': turn['observation']}, ensure_ascii=False)}]
            recovered = {'action': 'check_query', 'args': {'prediction': checked['candidate']}}
            messages += [{'role': 'assistant', 'content': json.dumps(recovered, ensure_ascii=False)},
                         {'role': 'user', 'content': json.dumps({'observation': checked,
                            'instruction': '之前响应缺少结尾括号，程序仅补结尾括号后校验通过，字段和条件没有改动。请用candidate调用finish。输出多行缩进JSON，逐层闭合对象。'}, ensure_ascii=False)}]
            state = {'step': len(record['attempts']), 'initial_call_count': len(record['attempts']),
                     'messages': messages, 'check': checked, 'suffixes': [suffix], 'extra_attempts': [], 'trace': []}
            state['trace'].append({'step': len(record['trace']), 'action': 'check_query',
                'args': recovered['args'], 'observation': checked, 'origin': 'automatic_json_closure'})
        else:
            state = json.loads(state_path.read_text())
            tools.accepted = state['check']
        response_path = args.run / f"{row['question_id']}.response.{state['step']}.json"
        if response_path.exists():
            response = json.loads(response_path.read_text())
            choice = response['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise ValueError('恢复响应被截断，禁止仅补结尾处理')
            content = choice['message']['content']
            envelope, suffix = close_json(content)
            state['suffixes'].append(suffix)
            observation = tools.dispatch(envelope['action'], envelope['args'])
            state['extra_attempts'].append({'content': content, 'finish_reason': choice.get('finish_reason'), 'usage': response.get('usage')})
            state['trace'].append({'step': len(record['trace']) + len(state['trace']),
                                  'action': envelope['action'], 'args': envelope['args'], 'observation': observation,
                                  'origin': 'model_json_recovery'})
            if observation.get('ok') and observation.get('finished'):
                record.update(prediction=observation['prediction'], model_prediction=observation['candidate'], errors=[],
                    prediction_source='react_model_with_automatic_json_closure',
                    automatic_transforms=sorted(set(observation['automatic_transforms'] + ['automatic_json_closure'])),
                    api_calls=record['api_calls'] + len(state['extra_attempts']),
                    attempts=record['attempts'] + state['extra_attempts'], trace=record['trace'] + state['trace'],
                    format_recovery={'method': 'append_missing_final_delimiters_only', 'suffixes': state['suffixes'],
                        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        'original_record': str(archive / path.name), 'manual_query_edits': False})
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                continue
            if len(state['extra_attempts']) >= 5:
                raise ValueError('格式恢复后模型仍未完成，恢复次数已达上限')
            state['messages'] += [{'role': 'assistant', 'content': content},
                                  {'role': 'user', 'content': json.dumps({'observation': observation,
                                     'instruction': '解决错误，保持已检查候选的字段与条件，输出合法动作JSON。'}, ensure_ascii=False)}]
            state['check'] = tools.accepted
            state['step'] += 1
        request_path = args.run / f"{row['question_id']}.request.{state['step']}.json"
        response_path = args.run / f"{row['question_id']}.response.{state['step']}.json"
        write_offline_request(request_path, response_path, completion_payload(config['model'], state['messages'], config['provider']))
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    records = [json.loads((args.run / (str(row['question_id']) + '.json')).read_text())
               for row in json.loads((args.run / 'sample.json').read_text())]
    metrics = summarize(records)
    (args.run / 'metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n')
    predictions = [{'question_id': record['question_id'], **record['prediction']}
                   for record in records if record.get('prediction') and not record.get('errors')]
    (args.run / 'predictions.json').write_text(json.dumps(predictions, ensure_ascii=False, indent=2) + '\n')
    build(args.run, args.base_url, args.auth_header_file)
    print(json.dumps({'recovery_ids': ids, 'metrics': metrics}, ensure_ascii=False))


if __name__ == '__main__':
    main()
