"""Observable prediction diagnostics; no causal claims from price alone."""
def diagnose(decision,entry,exit_price,index_entry,index_exit,bps):
    sign=1 if decision=='买入' else -1
    move=(exit_price/entry-1)*sign
    net=move-bps/10000
    index_direction=(index_exit/index_entry-1)*sign if index_entry and index_exit else None
    if net>0:
        category='方向兑现'
        explanation='截至预设观察终点，ETF方向变化覆盖了冻结的成本假设。'
    elif move>0:
        category='涨跌幅不足以覆盖成本'
        explanation='价格方向正确，但变化幅度未覆盖冻结成本；提示过度交易风险，不等于实际成交亏损。'
    elif index_direction is not None and index_direction>0:
        category='指数与ETF方向不一致'
        explanation='指数方向符合预期，ETF未兑现。可排查折溢价、跟踪误差、报价时差或分红；仅为排查线索。'
    else:
        category='市场方向未兑现'
        explanation='指数与ETF均未支持该方向。可复核趋势过滤、支撑压力和触发时机；不能仅凭价格证明错误的因果。'
    return dict(category=category,explanation=explanation,net=net,etf_direction=move,index_direction=index_direction)
