import pdfplumber
import re
import sys

def extract_pdf_content(pdf_path):
    """提取PDF文件内容"""
    content = ""
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text:
                    content += f"=== 第{i+1}页 ===\n"
                    content += text + "\n\n"
        
        return content
    except Exception as e:
        return f"提取PDF时出错: {str(e)}"

def summarize_project_bonus(content):
    """总结项目奖金执行细则内容"""
    summary = []
    
    # 提取标题
    title_match = re.search(r'([^\n]+项目奖金[^\n]+)', content)
    if title_match:
        summary.append(f"文档标题: {title_match.group(1)}")
    
    # 提取章节
    sections = re.findall(r'第[一二三四五六七八九十\d]+[章节条]\s*[^\n]+', content)
    if sections:
        summary.append("\n主要章节:")
        for section in sections[:15]:  # 显示更多章节
            summary.append(f"  - {section.strip()}")
    
    # 查找关键信息
    summary.append("\n关键条款:")
    
    # 奖金计算相关
    if '奖金 =' in content or '奖金=' in content:
        bonus_calc = re.search(r'奖金\s*[=＝]\s*[^\n]+', content)
        if bonus_calc:
            summary.append(f"  奖金计算公式: {bonus_calc.group()[:100]}")
    
    # 分配比例
    percentages = re.findall(r'(\d+%)\s*[^\n]{0,50}', content)
    if percentages:
        unique_percentages = list(set(percentages))
        summary.append(f"  涉及分配比例: {', '.join(unique_percentages[:15])}")
    
    # 适用人员
    if '适用人员' in content or '适用范围' in content:
        scope_match = re.search(r'(适用[人员范围][^。]{0,100})', content)
        if scope_match:
            summary.append(f"  适用人员范围: {scope_match.group(1)[:150]}")
    
    # 发放时间
    if '发放' in content:
        payment_match = re.search(r'(发放[^。]{0,80})', content)
        if payment_match:
            summary.append(f"  发放安排: {payment_match.group(1)[:100]}")
    
    # 考核标准
    if '考核' in content:
        assessment_match = re.search(r'(考核[^。]{0,80})', content)
        if assessment_match:
            summary.append(f"  考核标准: {assessment_match.group(1)[:100]}")
    
    # 提取重要数字
    amounts = re.findall(r'[¥￥$]\s*\d+[,\d]*\.?\d*|\d+[,\d]*\.?\d*\s*[万元]', content)
    if amounts:
        summary.append(f"\n涉及金额: {', '.join(set(amounts[:10]))}")
    
    # 统计页数
    page_count = len(re.findall(r'=== 第\d+页 ===', content))
    summary.append(f"\n文档页数: {page_count}页")
    
    # 提取发布日期
    date_match = re.search(r'(\d{4}[年\-]\d{1,2}[月\-]\d{1,2}日?)', content)
    if date_match:
        summary.append(f"发布日期: {date_match.group(1)}")
    
    # 提取审批信息
    if '审批' in content or '批准' in content:
        approval_match = re.search(r'([审批批准][^。]{0,50})', content)
        if approval_match:
            summary.append(f"审批信息: {approval_match.group(1)}")
    
    return "\n".join(summary)

def main():
    pdf_path = r"D:\聊天记录\WXWork\1688858083551093\Cache\File\2024-09\2024年项目奖金执行细则-20240926.pdf"
    
    print("正在提取PDF内容...")
    content = extract_pdf_content(pdf_path)
    
    if "出错" in content:
        print(content)
        return
    
    # 设置控制台编码为UTF-8
    sys.stdout.reconfigure(encoding='utf-8')
    
    print("\n" + "="*80)
    print("2024年项目奖金执行细则 - 内容总结")
    print("="*80)
    
    summary = summarize_project_bonus(content)
    print(summary)
    
    # 保存完整内容到文件
    with open("pdf_content_utf8.txt", "w", encoding="utf-8") as f:
        f.write(content)
    
    print("\n" + "="*80)
    print(f"完整内容已保存到: pdf_content_utf8.txt")
    print(f"提取了约 {len(content)} 个字符")
    
    # 显示部分内容预览
    print("\n" + "="*80)
    print("内容预览（前1000字符）:")
    print("="*80)
    print(content[:1000])

if __name__ == "__main__":
    main()