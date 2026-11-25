"""Resolve the model identifiers accepted by the workbench."""
MODELS={level:'gemini-3.8-flash-'+level for level in ('low','medium','high')}
DEFAULT=MODELS['medium']
FLASH_FAMILY='gemini-3.8-flash'
NEON_FAMILY='gemini-4-neon'
HYDROGEN_FAMILY='gemini-4-hydrogen'
DEFAULT_TEXT_MODEL=HYDROGEN_FAMILY+'-medium'
THOUGHT_LEVELS=('low','medium','high','xhigh')
ADVANCED_FAMILIES={NEON_FAMILY:'Gemini 4 Neon',HYDROGEN_FAMILY:'Gemini 4 Hydrogen'}
ADVANCED_MODELS={f'{family}-{level}' for family in ADVANCED_FAMILIES for level in THOUGHT_LEVELS}


def resolve(model=None,effort=None):
    if isinstance(model,str):
        if model in ADVANCED_FAMILIES or model in ADVANCED_MODELS:
            family,level=(model,'high') if model in ADVANCED_FAMILIES else model.rsplit('-',1)
            if effort is not None and (effort not in THOUGHT_LEVELS or (model in ADVANCED_MODELS and level!=effort)):
                raise ValueError('模型与思考等级不一致或超出可用范围。')
            return f'{family}-{effort or level}'
        if model == FLASH_FAMILY:
            model=DEFAULT
    if model not in (None,'',DEFAULT,*MODELS.values()):raise ValueError('请选择工作台支持的 Gemini 模型。')
    if effort is not None and (not isinstance(effort,str) or effort not in MODELS):
        return DEFAULT
    if effort and model not in (None,'',DEFAULT,MODELS[effort]):raise ValueError('model 与 effort 不一致。')
    return MODELS[effort] if effort else model or DEFAULT


def text_model_options(advanced=()):
    families={FLASH_FAMILY:'Gemini 3.8 Flash',**ADVANCED_FAMILIES}
    available=set(MODELS.values()) | set(advanced)
    return [{'id':model,'family':family,'label':label,'effort':effort}
            for family,label in families.items() for effort in THOUGHT_LEVELS
            if (model:=f'{family}-{effort}') in available]


def resolve_flash_fallback(model):
    if model is None or model is False:
        return model
    if model not in (FLASH_FAMILY,*MODELS.values()):
        raise ValueError('fallback_model 必须为受支持的 Flash 型号，或设为 false 关闭保底。')
    return resolve(model)
