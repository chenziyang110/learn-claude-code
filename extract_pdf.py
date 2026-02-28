import pdfplumber
import re
from collections import defaultdict

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
        for section in sections[:10]:  # 只显示前10个主要章节
            summary.append(f"  - {section.strip()}")
    
    # 查找关键信息
    keywords = {
        '奖金': '奖金相关条款',
        '分配': '奖金分配规则',
        '比例': '奖金比例',
        '考核': '考核标准',
        '项目': '项目类型',
        '金额': '奖金金额',
        '发放': '发放时间',
        '条件': '发放条件'
    }
    
    summary.append("\n关键信息:")
    for keyword, description in keywords.items():
        if keyword in content:
            # 找到包含关键词的句子
            sentences = re.findall(r'[^。]*' + keyword + r'[^。]*。', content)
            if sentences:
                summary.append(f"  - {description}: {sentences[0][:100]}...")
    
    # 提取数字和百分比
    percentages = re.findall(r'\d+%', content)
    if percentages:
        summary.append(f"\n涉及百分比: {', '.join(set(percentages[:10]))}")
    
    # 提取金额
    amounts = re.findall(r'[¥￥]\s*\d+[,\d]*\.?\d*|\d+[,\d]*\.?\d*\s*元', content)
    if amounts:
        summary.append(f"涉及金额: {', '.join(set(amounts[:10]))}")
    
    # 统计页数
    page_count = len(re.findall(r'=== 第\d+页 ===', content))
    summary.append(f"\n文档页数: {page_count}页")
    
    # 提取发布日期
    date_match = re.search(r'(\d{4}[年\-]\d{1,2}[月\-]\d{1,2}日?)', content)
    if date_match:
        summary.append(f"发布日期: {date_match.group(1)}")
    
    return "\n".join(summary)

def main():
    pdf_path = r"D:\聊天记录\WXWork\1688858083551093\Cache\File\2024-09\2024年项目奖金执行细则-20240926.pdf"
    
    print("正在提取PDF内容...")
    content = extract_pdf_content(pdf_path)
    
    if "出错" in content:
        print(content)
        return
    
    print("\n" + "="*80)
    print("PDF内容总结:")
    print("="*80)
    
    summary = summarize_project_bonus(content)
    print(summary)
    
    # 保存完整内容到文件
    with open("pdf_content.txt", "w", encoding="utf-8") as f:
        f.write(content)
    
    print("\n" + "="*80)
    print(f"完整内容已保存到: pdf_content.txt")
    print(f"提取了约 {len(content)} 个字符")

if __name__ == "__main__":
    main()