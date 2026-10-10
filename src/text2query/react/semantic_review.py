"""An evidence-grounded LLM review, separate from generation and label agreement."""
import hashlib
import json
from ..shared.generation import completion_payload, parse_output, write_offline_request
from ..shared.data import load

REVIEW_SYSTEM = '''你是独立SQL语义复核员，按当前问题、真实Schema和检索知识检查candidate。
same_question_cases是原始案例而非正确性标准。不要为追求案例一致而撤回更符合题意的修正；候选照抄错误案例也必须指出。
只要求修正有具体证据支持的实质问题，避免凭常识新增题目未要求的筛选；不因别名、排版、合理输出形式不同而否决。
检查统计对象、返回信息、粒度、关联、范围、表达式、枚举、分母和排序。这些判断由你完成，不是程序硬编码规则。
特别核对候选SQL的实际行为与改动说明是否一致，不能仅看说明接受查询。
表/字段具体证据优先于库级泛化描述；资料未区分多种格式或业务口径时，不凭猜测否决案例采用的格式。
证据不足以确定关键口径时用uncertain，标明缺少什么信息；不要把缺少真实数据当作所有问题都无法判断。
输出完整JSON对象：{"verdict":"accept|revise|uncertain","issues":[{"aspect":"问题方面","problem":"具体问题","evidence":"问题原文/Schema/knowledge_id等实际证据"}],"guidance":"简短修正建议或不确定性说明"}。
accept表示未发现有证据支持的实质错误；revise必须有非空issues和具体证据；uncertain保留歧义，不能当作证明正确。'''

# Eligibility of evidence is enforced by the tools, not a file-reading prompt.
RAW_REVIEW_SYSTEM = REVIEW_SYSTEM


class SemanticReviewer:
    def __init__(self, directory, question_id, model, base_url, provider, call, offline=False, raw_only=False):
        self.directory, self.question_id = directory, question_id
        self.model, self.base_url, self.provider = model, base_url, provider
        self.call, self.offline, self.calls = call, offline, 0
        self.raw_only = raw_only

    def __call__(self, evidence):
        # Keep source data intact on disk; repeated metadata/identical raw copies
        # add no new evidence to this independent review.
        compact = {**evidence}
        compact['schema'] = [{k: t[k] for k in ('source', 'database', 'table', 'raw_schema', 'ddl', 'table_comments', 'provenance') if k in t}
                             for t in evidence['schema']]
        compact['knowledge'] = [{k: row[k] for k in ('knowledge_id', 'knowledge_text', 'raw_annotation',
                                  'reviewed_knowledge_text', 'provenance') if k in row} for row in evidence['knowledge']]
        system = RAW_REVIEW_SYSTEM if self.raw_only else REVIEW_SYSTEM
        payload = completion_payload(self.model, [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(compact, ensure_ascii=False)}], self.provider)
        digest = hashlib.sha256(json.dumps({'base_url': self.base_url, 'payload': payload}, sort_keys=True).encode()).hexdigest()[:20]
        stem = f'{self.question_id}.semantic_review.{digest}'
        request, response_path = self.directory / (stem + '.request.json'), self.directory / (stem + '.response.json')
        write_offline_request(request, response_path, payload)
        if response_path.exists():
            response = load(response_path)
        elif self.offline:
            raise ValueError('独立语义复核响应尚未提供：' + response_path.name)
        else:
            self.calls += 1
            response = self.call(self.base_url, '/chat/completions', payload)
            response_path.write_text(json.dumps(response, ensure_ascii=False) + '\n')
        content = response['choices'][0]['message'].get('content') or ''
        result = parse_output(content)
        if result.get('verdict') not in ('accept', 'revise', 'uncertain') or not isinstance(result.get('issues'), list):
            raise ValueError('独立语义复核返回格式无效')
        if result['verdict'] == 'revise' and (not result['issues'] or any(
                not isinstance(issue, dict) or any(not isinstance(issue.get(key), str) or not issue[key].strip()
                for key in ('aspect', 'problem', 'evidence')) for issue in result['issues'])):
            raise ValueError('独立语义复核要求修正却未提供具体问题及证据')
        return {**result, 'request_file': request.name, 'response_file': response_path.name}
