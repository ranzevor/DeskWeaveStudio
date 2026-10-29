"""集合分页的筛选、游标绑定、快照与返回数据检查。"""
from __future__ import annotations

import json
import unicodedata


def normalized_filter(value):
    result = {}
    for key, entry in value.items():
        if entry == '' or entry == [] or key == 'minimumScore' and entry == 0:
            continue
        result[key] = sorted(set(entry), key=lambda item: item.encode('utf-16-be')) if isinstance(entry, list) else unicodedata.normalize('NFC', entry) if isinstance(entry, str) else entry
    return result


def matches(kind, row, filters):
    text = unicodedata.normalize('NFC', filters.get('text', '')).casefold()
    fields = {'diagnostics': ['code', 'message'], 'runRequirements': ['name'], 'templateNames': ['name'], 'templateAssets': ['name'], 'ocrLanguages': ['language']}
    if text and not any(text in unicodedata.normalize('NFC', str(row.get(key, ''))).casefold() for key in fields.get(kind, [])):
        return False
    for argument, field in [('codes', 'code'), ('states', 'state'), ('sources', 'source'), ('kinds', 'kind')]:
        if filters.get(argument) and row.get(field) not in filters[argument]:
            return False
    for key in ['nodeId', 'parentId', 'satisfied', 'conflict', 'candidateId', 'reused', 'scale']:
        if key in filters and row.get(key) != filters[key]:
            return False
    return row.get('score', 1) >= filters.get('minimumScore', 0)


def query_errors(value, kind, context=None):
    errors = []
    if kind == 'producer':
        for count, field in [('diagnosticCount', 'diagnosticsQuery'), ('requirementCount', 'requirementsQuery'), ('frameCount', 'framesQuery'), ('effectCount', 'effectsQuery'), ('itemCount', 'itemsQuery'), ('assetCount', 'assetsQuery'), ('candidateCount', 'candidatesQuery'), ('objectCount', 'objectsQuery'), ('installedLanguageCount', 'installedLanguagesQuery')]:
            if count in value and field in value and value[count] != value[field]['itemCount']:
                errors.append('集合总数与查询入口不一致')
        if 'conflictCount' in value:
            if value['conflictCount'] > value['itemCount'] or value['canCommit'] != (value['conflictCount'] == 0):
                errors.append('命名冲突总数与提交状态不一致')
        if value.get('outcome') == 'matched' and value['selectedIndex'] >= value['candidateCount']:
            errors.append('选中位置超出完整候选集合')
        return errors
    if kind == 'close':
        return errors
    if context is None:
        return ['缺少查询上下文']
    params = value if kind == 'request' else context['params']
    source = context.get('source', {})
    if source and any(params[key] != source[key] for key in ['collectionId', 'kind']):
        errors.append('查询集合身份或类型不匹配')
    if context.get('authorized') is False:
        errors.append('来源对象读取权已失效')
    now = context.get('now')
    if now and source.get('expiresAt') and now >= source['expiresAt']:
        errors.append('来源集合已过期')
    binding = context.get('cursorBinding')
    if params.get('cursor') is not None:
        if not binding or params['cursor'] != binding.get('cursor'):
            errors.append('游标无效')
        else:
            if any(context.get(key) != binding.get(key) for key in ['principal', 'workspace', 'authorizationVersion']):
                errors.append('游标不属于当前读取者、工作空间或授权版本')
            if any(params[key] != binding[key] for key in ['collectionId', 'kind']):
                errors.append('游标来源发生变化')
            if normalized_filter(params.get('filter', {})) != normalized_filter(binding.get('filter', {})) or params.get('order', 'asc') != binding.get('order', 'asc') or params.get('pageSize', 50) != binding.get('pageSize', 50):
                errors.append('续页条件与原查询不一致')
            if now and now >= binding['expiresAt']:
                errors.append('查询游标已过期')
    if kind == 'request':
        return errors
    if kind != 'page':
        return errors + ['未知查询检查类型']
    if any(value[key] != params[key] for key in ['collectionId', 'kind']):
        errors.append('返回页不属于当前查询')
    if len(value['items']) > params.get('pageSize', 50):
        errors.append('返回数量超过请求页大小')
    if params.get('includeTotal') and value['matchedCount'] is None:
        errors.append('请求总数但未返回固定结果的准确总数')
    snapshot = value['querySnapshot']
    if snapshot['expiresAt'] <= snapshot['snapshotAt'] or now and now >= snapshot['expiresAt']:
        errors.append('查询快照期限不合法')
    if source.get('expiresAt') and snapshot['expiresAt'] > source['expiresAt']:
        errors.append('查询期限超过来源集合期限')
    expected = context.get('querySnapshot')
    if expected and snapshot != expected:
        errors.append('续页改变了查询快照或有效期')
    rows = context.get('rows')
    if rows is not None:
        indices = [i for i, row in enumerate(rows) if matches(params['kind'], row, params.get('filter', {}))]
        if params.get('order', 'asc') == 'desc':
            indices.reverse()
        if value['matchedCount'] is not None and value['matchedCount'] != len(indices):
            errors.append('筛选总数与固定成员不一致')
        if binding:
            last = binding['lastIndex']
            indices = [i for i in indices if i < last] if params.get('order', 'asc') == 'desc' else [i for i in indices if i > last]
        returned = [row['index'] for row in value['items']]
        if returned != indices[:len(returned)] or len(returned) != len(set(returned)):
            errors.append('分页次序重复、跳过成员或使用了其他数据版本')
        for item in value['items']:
            index = item['index']
            if index >= len(rows) or item['value'] != rows[index]:
                errors.append('页内容与固定来源不一致')
        remaining = len(indices) > len(returned)
        if (value['nextCursor'] is not None) != remaining or remaining and not returned:
            errors.append('下一游标或末页状态不正确')
    if len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 262144:
        errors.append('查询页超过 256KiB')
    return errors


def catalog_errors(schemas, catalog):
    errors = []
    service = schemas['service.schema.json']['$defs']
    methods = {row['method']: row for row in catalog['tools']}
    for name in ['query.read', 'query.close']:
        if name not in methods or name not in service:
            errors.append('集合查询方法未发布')
    for method in ['workspace.catalog', 'template.candidates', 'observation.query']:
        params = service[method]['properties']['params']
        if params['properties']['pageSize'] != {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50} or {'cursor', 'pageSize'} & set(params['required']):
            errors.append(method + ' 的分页默认值不一致')
        if service[method]['properties']['result']['properties']['items']['maxItems'] != 100:
            errors.append(method + ' 的查询页未限制数量')
    forbidden = {'workflow.validate': ['diagnostics'], 'run.prepare': ['requirements', 'diagnostics'], 'run.snapshot': ['frames', 'effects', 'framesResource', 'effectsResource'], 'template.names': ['items'], 'ocr.status': ['installedLanguages']}
    ids = {schema['$id']: schema for schema in schemas.values()}
    def resolve(value):
        while '$ref' in value:
            uri, _, pointer = value['$ref'].partition('#')
            value = ids[uri]
            for part in pointer.lstrip('/').split('/') if pointer else []:
                value = value[part.replace('~1', '/').replace('~0', '~')]
        return value
    for method, names in forbidden.items():
        if set(names) & set(resolve(service[method]['properties']['result'])['properties']):
            errors.append(method + ' 仍返回完整集合')
    for method, field in [('vision.match', 'candidates'), ('template.commit', 'assets'), ('asset.import', 'objects')]:
        result = resolve(schemas['operation-results.schema.json']['$defs'][method])
        if field in result['properties']:
            errors.append('异步结果仍返回完整集合：' + method)
    for row in catalog['collectionQueries']:
        value = service[row['method']]['properties']['result'] if 'method' in row else schemas['operation-results.schema.json']['$defs'][row['operation']]
        value = resolve(value)
        expected = value['properties'][row['field']]
        if expected['allOf'][1]['properties']['kind'].get('const') != row['kind']:
            errors.append('集合查询类型不一致')
    return errors
