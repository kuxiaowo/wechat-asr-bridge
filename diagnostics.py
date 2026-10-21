"""Readable errors shared by the API, native backends and desktop client."""


def exception_detail(error):
    parts, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        part = f'{type(error).__name__}: {error}'
        if part not in parts:
            parts.append(part)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return '\n原因：'.join(parts)


def format_diagnostics(state):
    """Keep complete messages available without fitting them into a status label."""
    if not state:
        return '尚未取得服务诊断。'
    lines = []
    for pool in state.get('pools', [state]):
        title = pool.get('name') or pool.get('backend') or '服务'
        lines.append(f'【{title}】')
        detail = pool.get('last_error_detail')
        if isinstance(detail, dict):
            lines.append(f"错误 [{detail.get('code', 'unknown')}]：{detail.get('message', '')}")
            if detail.get('details'):
                lines.append(str(detail['details']))
            if detail.get('request_id'):
                lines.append('请求 ID：' + detail['request_id'])
        elif pool.get('last_error'):
            lines.append('错误：' + str(pool['last_error']))
        warnings = pool.get('warnings') or []
        for warning in warnings:
            lines.append(f"警告 [{warning.get('code', 'warning')}]：{warning.get('message', '')}")
        if not pool.get('last_error') and not detail and not warnings:
            lines.append('没有错误或警告。')
        lines.append('')
    return '\n'.join(lines).strip()
