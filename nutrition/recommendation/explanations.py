"""Rule-derived explanations without medical claims."""

def build_explanation(food_name, portion_g, contribution, components):
    improved = [key for key, value in components.items()
                if value["normalized_gap_improvement"] > 0]
    overshot = [key for key, value in components.items() if value["incremental_overshoot"] > 0]
    text = (f"{food_name} 在 {portion_g} g 份量下预计提供 "
            f"{contribution['energy']} kcal、蛋白质 {contribution['protein']} g、"
            f"脂肪 {contribution['fat']} g、碳水化合物 {contribution['carbohydrates']} g。")
    if improved:
        text += " 计算显示改善的目标项：" + "、".join(improved) + "。"
    if overshot:
        text += " 需注意新增超额项：" + "、".join(overshot) + "。"
    return text
