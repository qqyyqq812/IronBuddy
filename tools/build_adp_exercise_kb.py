#!/usr/bin/env python3
"""Build ADP-importable coach knowledge packs for Lane A.

The pack is text-only: no images, no videos, no secrets.  It uses wger's public
exercise API as the primary open exercise source, USDA FoodData Central for a
small nutrition seed set when available, and IronBuddy-owned coach policy
documents so ADP can answer planning, nutrition, fatigue, risk, and report
questions through one knowledge base.
"""
from __future__ import print_function

import csv
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "docs", "给用户看的交付", "LaneA_智能健身助手", "adp_import")
SOURCE_DATA_DIR = os.path.join(OUT_DIR, "source_data")
DEFAULT_LIMIT = 200
WGER_URL = "https://wger.de/api/v2/exerciseinfo/"
FDC_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
ADP_FAQ_HEADERS = [
    "1级分类", "2级分类", "3级分类", "4级分类", "5级分类",
    "6级分类", "7级分类", "8级分类", "9级分类", "10级分类",
    "问题（必填）", "答案（必填）", "问题描述（非必填）",
    "相似问（非必填）", "有效期（非必填）", "自定义参数（非必填）",
    "生效范围（非必填）",
]


def _clean_html(value):
    value = value or ""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<li\s*>", "- ", value, flags=re.I)
    value = re.sub(r"</li\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = value.replace("\u200b", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "IronBuddy/1.0 ADP-KB-builder"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _english_translation(item):
    translations = item.get("translations") or []
    for trans in translations:
        if trans.get("language") == 2:
            return trans
    return translations[0] if translations else {}


def _names(values, key="name"):
    out = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        name = value.get("name_en") or value.get(key) or value.get("name")
        if name:
            out.append(str(name))
    return out


def _record_from_wger(item):
    trans = _english_translation(item)
    name = (trans.get("name") or "").strip()
    desc = _clean_html(trans.get("description_source") or trans.get("description") or "")
    if not name or not desc:
        return None
    primary = _names(item.get("muscles"))
    secondary = _names(item.get("muscles_secondary"))
    equipment = _names(item.get("equipment"))
    category = ((item.get("category") or {}).get("name") or "").strip()
    license_info = item.get("license") or {}
    author = trans.get("license_author") or item.get("license_author") or ""
    source_id = "wger:%s" % item.get("id")
    query = "怎么做%s？%s 注意事项 %s" % (name, category, " ".join(primary[:3]))
    answer = (
        "动作：%s\n"
        "分类：%s\n"
        "主要肌群：%s\n"
        "辅助肌群：%s\n"
        "器械：%s\n"
        "执行要点：\n%s\n\n"
        "IronBuddy 教练提示：先用可控速度和无痛范围练习；如果出现疼痛、眩晕、刺痛、肿胀或动作明显失控，应降低强度或停止本组。本建议用于训练辅助，不替代医疗诊断。"
    ) % (
        name,
        category or "未分类",
        ", ".join(primary) or "未标注",
        ", ".join(secondary) or "未标注",
        ", ".join(equipment) or "未标注",
        desc,
    )
    return {
        "id": source_id,
        "question": query,
        "answer": answer,
        "title": name,
        "source": "wger exercise API",
        "license": license_info.get("short_name") or license_info.get("full_name") or "",
        "license_url": license_info.get("url") or "",
        "license_author": author,
        "category": category,
    }


def fetch_wger_records(limit):
    records = []
    offset = 0
    page_size = min(100, max(1, limit))
    while len(records) < limit:
        params = urllib.parse.urlencode({"limit": page_size, "offset": offset, "language": 2})
        try:
            data = _fetch_json(WGER_URL + "?" + params)
        except Exception as exc:
            print("warning: wger fetch stopped after %d records: %s" % (len(records), exc), file=sys.stderr)
            break
        for item in data.get("results") or []:
            rec = _record_from_wger(item)
            if rec:
                records.append(rec)
            if len(records) >= limit:
                break
        if not data.get("next"):
            break
        offset += page_size
    return records


FDC_SEED_FOODS = [
    ("chicken breast", "鸡胸肉"),
    ("egg whole raw", "鸡蛋"),
    ("oats", "燕麦"),
    ("brown rice cooked", "糙米"),
    ("banana raw", "香蕉"),
    ("milk low fat", "低脂牛奶"),
    ("tofu firm", "北豆腐"),
    ("salmon raw", "三文鱼"),
    ("broccoli raw", "西兰花"),
    ("sweet potato raw", "红薯"),
]


def _nutrient_lookup(food):
    nutrients = {}
    for item in food.get("foodNutrients") or []:
        name = str(item.get("nutrientName") or "").lower()
        value = item.get("value")
        unit = item.get("unitName") or ""
        if value is None:
            continue
        if name == "energy":
            nutrients["energy"] = "%s %s" % (value, unit)
        elif name == "protein":
            nutrients["protein"] = "%s %s" % (value, unit)
        elif name in ("carbohydrate, by difference", "carbohydrate"):
            nutrients["carbohydrate"] = "%s %s" % (value, unit)
        elif name in ("total lipid (fat)", "total fat"):
            nutrients["fat"] = "%s %s" % (value, unit)
        elif name in ("fiber, total dietary", "fiber"):
            nutrients["fiber"] = "%s %s" % (value, unit)
    return nutrients


def fetch_usda_food_records():
    records = []
    for query, zh_name in FDC_SEED_FOODS:
        url = FDC_SEARCH_URL + "?" + urllib.parse.urlencode({
            "api_key": os.environ.get("FDC_API_KEY", "DEMO_KEY"),
            "query": query,
            "pageSize": 1,
        })
        try:
            data = _fetch_json(url)
        except Exception:
            continue
        foods = data.get("foods") or []
        if not foods:
            continue
        food = foods[0]
        nutrients = _nutrient_lookup(food)
        desc = food.get("description") or query
        fdc_id = food.get("fdcId")
        answer = (
            "%s（USDA FoodData Central 条目：%s）可作为营养估算参考。"
            "常用营养字段：能量 %s，蛋白质 %s，碳水化合物 %s，脂肪 %s，膳食纤维 %s。"
            "IronBuddy 使用这些数据时只做训练饮食辅助：训练前优先保证易消化碳水和水分，"
            "训练后优先补充蛋白质、碳水和液体；如有疾病、过敏、减重或特殊饮食需求，应按医生或营养师建议调整。"
        ) % (
            zh_name,
            fdc_id or "unknown",
            nutrients.get("energy", "未标注"),
            nutrients.get("protein", "未标注"),
            nutrients.get("carbohydrate", "未标注"),
            nutrients.get("fat", "未标注"),
            nutrients.get("fiber", "未标注"),
        )
        records.append({
            "id": "usda:%s" % (fdc_id or re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")),
            "question": "%s的营养成分适合训练计划怎么用？" % zh_name,
            "answer": answer,
            "title": "%s 营养摘要" % zh_name,
            "source": "USDA FoodData Central",
            "license": "USDA public data",
            "license_url": "https://fdc.nal.usda.gov/",
            "license_author": "USDA",
            "category": "营养食物",
        })
    return records


USDA_NUTRIENT_IDS = {
    "1003": "protein",
    "1004": "fat",
    "1005": "carbohydrate",
    "1008": "energy",
    "1079": "fiber",
    "1093": "sodium",
    "2000": "sugar",
}


def _find_zip_member(zip_file, suffix):
    for name in zip_file.namelist():
        if os.path.basename(name) == suffix:
            return name
    return None


def fetch_usda_sr_legacy_records(limit=3000):
    zpath = os.path.join(SOURCE_DATA_DIR, "FoodData_Central_sr_legacy_food_csv_2018_04.zip")
    if not os.path.exists(zpath):
        return []
    records = []
    try:
        zf = zipfile.ZipFile(zpath)
    except Exception:
        return []
    with zf:
        food_member = _find_zip_member(zf, "food.csv")
        nutrient_member = _find_zip_member(zf, "food_nutrient.csv")
        category_member = _find_zip_member(zf, "food_category.csv")
        if not food_member or not nutrient_member:
            return []
        categories = {}
        if category_member:
            with zf.open(category_member) as f:
                reader = csv.DictReader((line.decode("utf-8-sig", "replace") for line in f))
                for row in reader:
                    categories[row.get("id") or ""] = row.get("description") or ""
        foods = {}
        with zf.open(food_member) as f:
            reader = csv.DictReader((line.decode("utf-8-sig", "replace") for line in f))
            for row in reader:
                fdc_id = row.get("fdc_id") or ""
                if not fdc_id:
                    continue
                foods[fdc_id] = {
                    "description": row.get("description") or "",
                    "category": categories.get(row.get("food_category_id") or "", ""),
                }
        nutrients_by_food = {}
        with zf.open(nutrient_member) as f:
            reader = csv.DictReader((line.decode("utf-8-sig", "replace") for line in f))
            for row in reader:
                nutrient_key = USDA_NUTRIENT_IDS.get(row.get("nutrient_id") or "")
                if not nutrient_key:
                    continue
                fdc_id = row.get("fdc_id") or ""
                if fdc_id not in foods:
                    continue
                try:
                    amount = float(row.get("amount") or "0")
                except Exception:
                    continue
                nutrients_by_food.setdefault(fdc_id, {})[nutrient_key] = amount
    for fdc_id, food in foods.items():
        if len(records) >= int(limit):
            break
        nutrients = nutrients_by_food.get(fdc_id) or {}
        if not nutrients:
            continue
        desc = food.get("description") or ""
        category = food.get("category") or "Food"
        if not desc:
            continue
        parts = []
        if "energy" in nutrients:
            parts.append("能量 %.0f kcal" % nutrients["energy"])
        if "protein" in nutrients:
            parts.append("蛋白质 %.1f g" % nutrients["protein"])
        if "carbohydrate" in nutrients:
            parts.append("碳水 %.1f g" % nutrients["carbohydrate"])
        if "fat" in nutrients:
            parts.append("脂肪 %.1f g" % nutrients["fat"])
        if "fiber" in nutrients:
            parts.append("膳食纤维 %.1f g" % nutrients["fiber"])
        if "sodium" in nutrients:
            parts.append("钠 %.0f mg" % nutrients["sodium"])
        if "sugar" in nutrients:
            parts.append("糖 %.1f g" % nutrients["sugar"])
        answer = (
            "USDA SR Legacy 食物条目：%s。类别：%s。每 100g 可食部分营养摘要：%s。"
            "用于 IronBuddy 时，可把它作为训练饮食估算：高蛋白食物适合恢复和增肌；"
            "高碳水食物适合训练前后补能；高脂或高糖食物不宜在临近训练前大量摄入。"
            "具体饮食仍需结合用户目标、过敏、疾病和总能量摄入。"
        ) % (desc, category, "，".join(parts) or "未标注")
        records.append({
            "id": "usda_sr:%s" % fdc_id,
            "question": "%s 的营养成分和训练饮食建议是什么？" % desc,
            "answer": answer,
            "title": desc,
            "source": "USDA FoodData Central SR Legacy",
            "license": "USDA public data",
            "license_url": "https://fdc.nal.usda.gov/download-datasets/",
            "license_author": "USDA",
            "category": "营养食物",
        })
    return records


def ironbuddy_records():
    base = [
        (
            "深蹲膝盖不舒服怎么办？",
            "如果深蹲时膝盖不舒服，先停止当前组或降低强度。检查脚尖和膝盖方向是否一致，避免膝盖内扣；缩小下蹲幅度，降低速度，优先保持躯干稳定。若疼痛持续、出现刺痛、肿胀或影响走路，应停止训练并咨询专业医生。IronBuddy 的建议属于训练辅助，不替代医疗诊断。",
            "IronBuddy 风险提醒",
            "训练安全",
        ),
        (
            "疲劳很高时还要继续训练吗？",
            "疲劳值很高时不建议硬冲次数。应该先延长休息、降低动作速度或减少目标次数；如果动作质量下降、呼吸明显紊乱、头晕或关节不适，应停止本组。训练计划应优先保证动作质量，再考虑训练量。",
            "IronBuddy 疲劳解释",
            "疲劳管理",
        ),
        (
            "IronBuddy 怎么安排今天的深蹲训练？",
            "今日计划应结合最近训练历史、当前动作质量和疲劳趋势。默认建议 3 组深蹲，以疲劳目标而不是固定次数作为推进标准；如果前一组动作质量下降，下一组降低目标或延长休息；如果动作稳定且疲劳可控，再逐步进阶。",
            "IronBuddy 训练计划原则",
            "训练计划",
        ),
        (
            "深蹲时膝盖内扣怎么调整？",
            "膝盖内扣通常说明髋部控制、足部稳定或动作速度存在问题。先降低深度和速度，让膝盖始终跟随脚尖方向；把注意力放在脚掌三点支撑、臀部向后坐、核心稳定。若无法控制，先减少次数或改为箱式深蹲，再逐步恢复完整深蹲。",
            "深蹲动作纠错",
            "动作纠错",
        ),
        (
            "深蹲时腰酸怎么办？",
            "先停止当前组，检查是否塌腰、过度前倾或核心没有收紧。下一组降低深度，保持胸廓和骨盆稳定，避免追求过低深度。如果腰酸持续、出现放射痛或麻木，应停止训练并寻求专业评估。",
            "深蹲风险提醒",
            "训练安全",
        ),
        (
            "深蹲下蹲多深才合适？",
            "合适深度取决于无痛范围和动作控制。优先做到膝盖脚尖方向一致、脚跟稳定、背部中立；在这些条件满足时，可以逐步接近大腿与地面平行。不要为了深度牺牲膝盖轨迹、腰背稳定或呼吸节奏。",
            "深蹲深度原则",
            "动作标准",
        ),
        (
            "深蹲速度应该快还是慢？",
            "训练初期和动作纠错阶段建议慢速可控，尤其是下蹲阶段。慢速能暴露膝盖内扣、重心漂移和核心松动等问题。动作稳定后，可以根据目标加入正常节奏或爆发起身，但仍要避免失控下落。",
            "深蹲节奏原则",
            "动作标准",
        ),
        (
            "弯举时肩膀代偿怎么办？",
            "肩膀代偿通常是重量过大或肘部位置不稳定。先降低重量或减少次数，保持上臂靠近身体，避免耸肩和身体后仰。动作目标是肘关节主导屈伸，而不是用肩膀和腰背把重量甩起来。",
            "弯举动作纠错",
            "动作纠错",
        ),
        (
            "弯举时手腕疼怎么办？",
            "先停止当前组，检查手腕是否过度弯曲或握法太紧。下一组保持手腕中立，降低重量，避免快速甩动。如果疼痛持续、刺痛或影响握力，应停止训练并咨询专业人员。",
            "弯举风险提醒",
            "训练安全",
        ),
        (
            "训练前应该怎么热身？",
            "热身应从低强度全身活动开始，再进入目标关节和动作模式。深蹲前可做髋、踝、膝的动态活动和几次浅深蹲；弯举前可做肩肘腕活动和轻重量试做。热身目标是提高控制感，不是提前疲劳。",
            "热身建议",
            "训练准备",
        ),
        (
            "一组训练后应该休息多久？",
            "休息时间应看动作质量和疲劳恢复。轻中等强度通常休息 60 到 120 秒；如果上一组动作明显变形、呼吸未恢复或疲劳值过高，应延长休息。IronBuddy 更关注下一组能否保持标准动作，而不是固定秒数。",
            "组间休息原则",
            "训练计划",
        ),
        (
            "什么时候应该停止本组？",
            "出现关节疼痛、头晕、胸闷、动作失控、明显代偿或连续多次不标准时，应停止本组。疲劳很高但动作仍可控时，可以降低目标或延长休息；一旦安全信号出现，优先停止。",
            "停止训练条件",
            "风险提醒",
        ),
        (
            "训练计划应该按次数还是按疲劳推进？",
            "固定次数适合简单记录，但个体当天状态会波动。IronBuddy 更适合按疲劳和动作质量推进：动作稳定且疲劳可控，可以完成目标；动作质量下降或疲劳过高，就降低目标、延长休息或结束训练。",
            "疲劳驱动计划",
            "训练计划",
        ),
        (
            "疲劳值上升很快说明什么？",
            "疲劳值快速上升通常表示动作负荷、节奏、休息或当日状态不匹配。应检查动作是否变慢、姿势是否失控、肌肉激活是否异常升高。处理方式是降低强度、增加休息，并观察下一组动作质量是否恢复。",
            "疲劳解释",
            "疲劳管理",
        ),
        (
            "疲劳值很低是不是训练没效果？",
            "疲劳值低不等于没效果。新手学习动作、恢复日、技术训练都可以保持低疲劳。判断训练价值要同时看动作质量、目标完成度和长期趋势，而不是单次疲劳值越高越好。",
            "疲劳解释",
            "疲劳管理",
        ),
        (
            "动作不标准时训练计划怎么调整？",
            "如果不标准动作增加，下一组应降低目标：减少次数、降低速度要求、缩小动作幅度或延长休息。连续不标准时，应结束训练或切换为技术练习。计划调整的核心是先恢复动作质量。",
            "动作质量驱动计划",
            "训练计划",
        ),
        (
            "IronBuddy 的飞书详报应该看什么？",
            "飞书详报应优先看三类信息：本次训练完成情况、动作质量和风险提醒、下一步建议。专业问答命中后，报告会把问题、教练回答和训练上下文合在一起，方便复盘而不是只看原始数字。",
            "飞书详报说明",
            "飞书报告",
        ),
        (
            "训练复盘应该怎么写？",
            "复盘应包括本次目标、完成情况、动作质量、疲劳变化、风险信号和下一次调整。好的复盘不是堆数据，而是把数据翻译成教练判断：今天适合进阶、维持还是降负荷。",
            "训练复盘原则",
            "训练报告",
        ),
        (
            "OpenClaw 周报推送应该包含什么？",
            "OpenClaw 周报应概括一周训练趋势、主要动作完成度、疲劳变化、风险提醒和下周建议。它的作用是把日常训练记录变成可读的教练复盘，让老师或用户快速看出系统持续工作的价值。",
            "OpenClaw 周报说明",
            "飞书报告",
        ),
        (
            "如果用户问专业健身问题，IronBuddy 应该怎么回答？",
            "IronBuddy 应先给清晰结论，再给可执行建议，最后说明风险边界。回答重点是训练指导本身，不是罗列 DOI 或数据库编号。只有当用户需要来源时，再简要说明依据来自知识库和训练上下文。",
            "专业回答风格",
            "教练工作流",
        ),
        (
            "什么时候需要提醒用户就医？",
            "出现持续疼痛、刺痛、肿胀、麻木、头晕、胸闷、呼吸异常或影响日常活动时，应明确建议停止训练并就医或咨询专业人员。IronBuddy 可以做训练辅助和风险提醒，但不能替代医疗诊断。",
            "就医提醒边界",
            "风险提醒",
        ),
        (
            "老师验收时如何说明 IronBuddy 的智能教练能力？",
            "可以说明三点：第一，它能结合训练动作、疲劳和历史状态生成计划；第二，它能回答专业训练问题并给出风险提醒；第三，它能把训练过程自动整理成飞书详报和周报，形成可复盘的教练闭环。",
            "验收说明",
            "产品说明",
        ),
        (
            "IronBuddy 怎么制定训练计划？",
            "IronBuddy 制定训练计划时先看用户目标、最近训练历史、动作质量、疲劳状态和风险信号。计划输出应包含今日目标、动作安排、组数建议、每组目标、休息建议、降级条件和停止条件。计划不是固定流水线，而是训练前给方案，用户采纳后再执行。",
            "训练计划工作流",
            "训练计划",
        ),
        (
            "训练计划什么时候应该降级？",
            "当上一组出现连续不标准、疲劳上升过快、关节不适、呼吸明显紊乱或用户主观疲劳很高时，下一组应该降级。降级方式包括减少目标次数、降低速度要求、缩小动作幅度、延长休息或结束训练。",
            "训练计划降级",
            "训练计划",
        ),
        (
            "训练计划什么时候可以进阶？",
            "只有在动作质量稳定、疲劳可控、无疼痛且用户恢复良好时，才考虑进阶。进阶可以是增加少量次数、提高控制要求、缩短少量休息或加入更难变式。一次只改变一个变量，避免训练量突然跳变。",
            "训练计划进阶",
            "训练计划",
        ),
        (
            "IronBuddy 怎么制定营养计划？",
            "营养计划先区分目标：增肌、减脂、维持体能或恢复。通用原则是保证足够水分、优质蛋白、适量碳水、蔬果和规律进食。训练日前后应关注能量和恢复；如果用户有疾病、过敏、特殊饮食或体重快速变化，必须建议咨询医生或营养师。",
            "营养计划工作流",
            "营养计划",
        ),
        (
            "训练前应该吃什么？",
            "训练前优先选择易消化、不过量的碳水和适量蛋白，例如香蕉、燕麦、酸奶、米饭或鸡蛋。距离训练越近，食物越应简单清淡。避免大量油脂、酒精和不熟悉的食物，以免影响动作表现和胃肠舒适度。",
            "训练前营养",
            "营养计划",
        ),
        (
            "训练后应该吃什么？",
            "训练后应补水，并补充优质蛋白和适量碳水，帮助恢复和适应训练。可选择鸡蛋、牛奶、鸡胸肉、豆腐、鱼肉、米饭、土豆、燕麦或水果。重点不是立刻吃很多，而是在当天总摄入里满足恢复需要。",
            "训练后营养",
            "营养计划",
        ),
        (
            "减脂时训练饮食怎么安排？",
            "减脂应保持轻中度能量缺口，同时保证蛋白质、蔬菜、睡眠和力量训练。不要用极端节食换短期体重下降；如果训练表现明显下降、头晕或恢复变差，应提高能量摄入或降低训练量。",
            "减脂营养",
            "营养计划",
        ),
        (
            "增肌时训练饮食怎么安排？",
            "增肌需要稳定训练刺激、足够蛋白质和适度能量盈余。建议把蛋白质分散到多餐，训练前后保证碳水和水分，避免只增加高油高糖食物。体重和围度应缓慢变化，训练质量应同步提高。",
            "增肌营养",
            "营养计划",
        ),
        (
            "IronBuddy 生成营养建议时不能做什么？",
            "IronBuddy 不能替代医生或注册营养师，不能诊断疾病，不能为糖尿病、肾病、进食障碍、孕期、药物治疗等高风险场景给强制处方。遇到这些情况，应给出一般安全原则并建议咨询专业人员。",
            "营养风险边界",
            "风险提醒",
        ),
        (
            "训练报告和营养计划应该怎样合并？",
            "合并报告应先总结训练完成度和疲劳，再说明恢复与营养建议。格式应清晰：本次训练、动作质量、疲劳解释、风险提醒、饮食恢复建议、下次计划。不要堆大量原始数字，要把数据转成教练判断。",
            "训练营养复盘",
            "训练报告",
        ),
    ]
    out = []
    for i, (q, a, title, category) in enumerate(base, 1):
        out.append({
            "id": "ironbuddy:%03d" % i,
            "question": q,
            "answer": a,
            "title": title,
            "source": "IronBuddy Lane A",
            "license": "Project-owned",
            "license_url": "",
            "license_author": "IronBuddy",
            "category": category,
        })
    return out


def _similar_questions(question):
    q = (question or "").strip()
    if not q:
        return ""
    variants = []
    if q.endswith("？"):
        variants.append(q[:-1])
    if "怎么办" in q:
        variants.append(q.replace("怎么办", "如何处理"))
    if "怎么" in q:
        variants.append(q.replace("怎么", "如何"))
    if "应该" in q:
        variants.append(q.replace("应该", "需要"))
    seen = []
    for item in variants:
        item = item.strip()
        if item and item != q and item not in seen:
            seen.append(item)
    return " ".join(seen[:3])


def write_adp_faq_xlsx(records):
    try:
        import xlsxwriter
    except Exception as exc:
        raise RuntimeError("xlsxwriter is required to write ADP XLSX: %s" % exc)

    xlsx_path = os.path.join(OUT_DIR, "ironbuddy_open_exercise_qa_adp_faq.xlsx")
    workbook = xlsxwriter.Workbook(xlsx_path)
    sheet = workbook.add_worksheet("FAQ")
    header_fmt = workbook.add_format({"bold": True, "bg_color": "#EAF2F8", "border": 1})
    wrap_fmt = workbook.add_format({"text_wrap": True, "valign": "top"})
    for col, header in enumerate(ADP_FAQ_HEADERS):
        sheet.write(0, col, header, header_fmt)
    sheet.set_column(0, 9, 10)
    sheet.set_column(10, 10, 36)
    sheet.set_column(11, 11, 80)
    sheet.set_column(12, 13, 40)
    sheet.set_column(14, 14, 20)
    sheet.set_column(15, 15, 35)
    sheet.set_column(16, 16, 25)
    for row, rec in enumerate(records, 1):
        source = rec.get("source") or ""
        category = rec.get("category") or "通用"
        if source == "IronBuddy Lane A":
            lv1 = "IronBuddy"
            lv2 = category[:10]
        elif source.startswith("USDA FoodData Central") or source.startswith("USDA "):
            lv1 = "营养库"
            lv2 = category[:10]
        else:
            lv1 = "动作库"
            lv2 = (category or "训练动作")[:10]
        desc = "标题：%s；来源：%s；许可：%s；作者：%s" % (
            rec.get("title") or "",
            source,
            rec.get("license") or "",
            rec.get("license_author") or "",
        )
        values = [""] * len(ADP_FAQ_HEADERS)
        values[0] = lv1[:10]
        values[1] = lv2[:10]
        values[10] = rec.get("question") or ""
        values[11] = rec.get("answer") or ""
        values[12] = desc[:1000]
        values[13] = _similar_questions(rec.get("question") or "")
        values[15] = json.dumps({
            "id": rec.get("id") or "",
            "source": source,
            "license": rec.get("license") or "",
        }, ensure_ascii=False)
        for col, value in enumerate(values):
            sheet.write(row, col, value, wrap_fmt)
    workbook.close()
    return xlsx_path


def _write_doc(path, title, sections):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n" % title)
        f.write("> IronBuddy Lane A ADP 文档知识库导入稿。本文档不含密钥、不含用户个人数据；用于训练计划、营养计划、疲劳解释、风险提醒和飞书复盘生成。\n\n")
        for heading, body in sections:
            f.write("## %s\n\n%s\n\n" % (heading, body.strip()))


def write_adp_documents(records):
    doc_dir = os.path.join(OUT_DIR, "docs_kb")
    if not os.path.isdir(doc_dir):
        os.makedirs(doc_dir)
    training_path = os.path.join(doc_dir, "ironbuddy_training_plan_kb.md")
    nutrition_path = os.path.join(doc_dir, "ironbuddy_nutrition_plan_kb.md")
    report_path = os.path.join(doc_dir, "ironbuddy_report_and_risk_kb.md")
    index_path = os.path.join(doc_dir, "README.md")

    _write_doc(training_path, "IronBuddy 训练计划与疲劳教练知识库", [
        ("教练目标", "IronBuddy 的训练计划目标是让用户在动作质量可控、风险可控、疲劳可解释的前提下完成训练。计划不应只追求次数，而应根据动作质量、疲劳趋势、用户目标和训练历史调整。"),
        ("计划输入", "生成计划时优先读取：用户目标、当前动作类型、最近训练记录、上一组完成情况、标准/不标准次数、疲劳值、疲劳增长速度、关节疼痛或不适、用户主观反馈。缺少某项数据时，应明确说明按默认保守方案生成。"),
        ("计划输出", "训练计划应包含：今日目标、动作安排、推荐组数、每组目标、组间休息、降级条件、停止条件和下一次复盘重点。用户采纳后才进入训练执行；未采纳时不应默认启动训练。"),
        ("疲劳驱动", "疲劳值用于解释训练负荷和恢复压力。疲劳高时应降低目标或延长休息；疲劳低但动作质量差时也不能进阶；疲劳低且动作稳定时可以小幅进阶。"),
        ("动作质量优先", "连续不标准、膝盖内扣、腰背失稳、肩膀代偿、手腕疼痛、呼吸紊乱和明显失控，都应优先触发降级或停止，而不是继续追求计划次数。"),
        ("进阶规则", "进阶只能在动作稳定、无疼痛、疲劳可控、恢复良好时发生。一次只改变一个变量，例如增加少量次数、提高控制要求或缩短少量休息。"),
        ("降级规则", "降级方式包括减少目标次数、降低速度要求、缩小动作幅度、延长休息、切换为技术练习或结束训练。降级说明要给出原因，让用户理解不是失败，而是保护训练质量。"),
    ])

    _write_doc(nutrition_path, "IronBuddy 营养计划知识库", [
        ("营养计划定位", "IronBuddy 的营养建议用于训练辅助和恢复建议，不替代医生、营养师或疾病治疗方案。回答应避免处方化，重点给出可执行、保守、清晰的饮食原则。"),
        ("目标分类", "营养计划先区分目标：增肌、减脂、维持体能、恢复。增肌关注足够蛋白和适度能量盈余；减脂关注轻中度能量缺口和蛋白质保留；恢复关注水分、碳水、蛋白质和睡眠。"),
        ("训练前饮食", "训练前适合选择易消化碳水和少量蛋白，例如香蕉、燕麦、酸奶、米饭、鸡蛋。距离训练越近，越应避免大量油脂、酒精和不熟悉食物。"),
        ("训练后饮食", "训练后优先补水，补充优质蛋白和适量碳水。可选食物包括鸡胸肉、鸡蛋、牛奶、豆腐、鱼肉、米饭、土豆、燕麦、水果和蔬菜。"),
        ("常见食物解释", "ADP FAQ 中的 USDA FoodData Central 条目提供常见食物的能量、蛋白质、碳水、脂肪和膳食纤维摘要。生成饮食建议时应把食物数据转化为训练建议，而不是只罗列营养表。"),
        ("风险边界", "糖尿病、肾病、心血管疾病、孕期、进食障碍、药物治疗、严重过敏和未成年人减重等场景，应建议咨询医生或营养师。"),
        ("Open Food Facts 后续能力", "包装食品和条码识别适合接 Open Food Facts 这类结构化库。若未来接入，应把条码查询结果摘要后交给 ADP，而不是全量导入所有包装食品数据。"),
    ])

    _write_doc(report_path, "IronBuddy 风险提醒、飞书报告与复盘知识库", [
        ("风险提醒原则", "风险提醒要明确、简短、可执行。出现疼痛、刺痛、肿胀、麻木、头晕、胸闷、呼吸异常、动作失控或影响日常活动时，应停止训练并建议咨询专业人员。"),
        ("飞书详报结构", "飞书详报应包含：训练目标、实际完成、动作质量、疲劳解释、风险提醒、专业问答摘要、下一步建议。报告应把训练数据翻译成教练判断，不应堆砌无意义数字。"),
        ("周报结构", "OpenClaw 周报应直接展示一周训练趋势、训练量变化、疲劳趋势、主要风险、训练计划调整和下周建议。它应像教练复盘，不像调试日志。"),
        ("专业回答风格", "回答先给结论，再给原因和步骤，最后给风险边界。不要把 DOI、PMID 或数据库编号当成产品结果；只有用户追问来源时才简要说明依据来自知识库。"),
        ("闭环定义", "完整闭环包括：读取训练历史和当前状态、生成训练计划、用户采纳、训练执行、疲劳解释、风险提醒、专业问答、飞书详报、周报复盘、下一次计划调整。"),
    ])

    with open(index_path, "w", encoding="utf-8") as f:
        f.write("# ADP 文档知识库导入目录\n\n")
        f.write("请在腾讯 ADP 知识库的“文档”页导入以下 Markdown 文件：\n\n")
        for path in (training_path, nutrition_path, report_path):
            f.write("- `%s`\n" % os.path.basename(path))
        f.write("\n同时在“问答”页导入上一层目录的 `ironbuddy_open_exercise_qa_adp_faq.xlsx`。\n")
    return [training_path, nutrition_path, report_path, index_path]


def write_outputs(records):
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    csv_path = os.path.join(OUT_DIR, "ironbuddy_open_exercise_qa.csv")
    jsonl_path = os.path.join(OUT_DIR, "ironbuddy_open_exercise_qa.jsonl")
    md_path = os.path.join(OUT_DIR, "README.md")
    xlsx_path = write_adp_faq_xlsx(records)
    doc_paths = write_adp_documents(records)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "question", "answer", "title", "source", "license",
            "license_url", "license_author", "category",
        ])
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# IronBuddy ADP Open Exercise KB 导入包\n\n")
        f.write("- 生成时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S %z"))
        f.write("- 记录数：%d\n" % len(records))
        f.write("- 主来源：wger public exercise API，动作文本保留来源、作者和许可字段。\n")
        f.write("- 营养来源：USDA FoodData Central 常见食物摘要；API 不可用时自动跳过。\n")
        f.write("- 补充来源：IronBuddy Lane A 自有训练计划、营养计划、疲劳解释、风险提醒、飞书报告问答。\n")
        f.write("- 问答导入：腾讯 ADP 知识库 -> 问答 -> 导入，上传 `ironbuddy_open_exercise_qa_adp_faq.xlsx`。\n")
        f.write("- 文档导入：腾讯 ADP 知识库 -> 文档 -> 导入，上传 `docs_kb/` 下的 Markdown 文件。\n")
        f.write("- 注意：本包不含图片、视频、密钥或用户个人数据。\n")
    return csv_path, jsonl_path, xlsx_path, md_path, doc_paths


def main(argv):
    limit = DEFAULT_LIMIT
    if len(argv) > 1:
        limit = int(argv[1])
    records = (
        ironbuddy_records()
        + fetch_usda_food_records()
        + fetch_usda_sr_legacy_records(limit=3000)
        + fetch_wger_records(limit)
    )
    paths = write_outputs(records)
    print("records:", len(records))
    for path in paths:
        if isinstance(path, list):
            for item in path:
                print(item)
        else:
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
