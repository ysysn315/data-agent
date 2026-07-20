from typing import List, Optional
from loguru import logger

class TableProcessor:
    """表格数据处理器 - 将表格转换为语义化文本"""
    @staticmethod
    def to_semantic_text(
        headers:List[str],
        rows:List[List[str]]=[],
        table_name:str="数据表",
        max_rows:int=100
    )->str:
        """
        将表格转换为语义化文本
        
        Args:
            headers: 列名列表
            rows: 数据行列表（每行是值的列表）
            table_name: 表格名称
            max_rows: 最大处理行数
        
        Returns:
            语义化的文本描述
        """
        if not headers or not rows:
            return f"[{table_name}]空表格"
        display_rows=rows[:max_rows] 
        total_rows=len(rows)
        truncated=total_rows>max_rows

        text_parts=[]

        text_parts.append(f"[{table_name}]")
        text_parts.append(f"表格包含{len(headers)}列：{','.join(headers)}")
        if truncated:
            text_parts.append(f"共{total_rows}条记录(仅展示前{max_rows}条)")
        else:
            text_parts.append(f"共{total_rows}条记录")
        text_parts.append('')

        for i,row in enumerate(display_rows,1):
            row_desc=TableProcessor._describe_row(headers,row,i)
            text_parts.append(row_desc)

        summary=TableProcessor._generate_summary(headers,rows)
        if summary:
            text_parts.append("")
            text_parts.append("统计摘要： ")
            text_parts.extend(summary)
        return "\n".join(text_parts)
    @staticmethod
    def _describe_row(headers: List[str], row: List[str], row_num: int) -> str:
        """将单行数据转换为自然语言描述"""
        values=row+['']*(len(headers)-len(row))
        pairs=[]
        for header,value in zip(headers,values):
            if value and str(value).strip():
                pairs.append(f"{header}为{value}")
        if pairs:
            return f"记录{row_num}: "+",".join(pairs)
        return f"记录{row_num}:(空记录)"
    @staticmethod
    def _generate_summary(headers:List[str],rows:List[List[str]])->List[str]:
        """生成统计摘要"""
        summary=[]

        summary.append(f"-共 {len(rows)}条记录")

        for i,header in enumerate(headers):
            values=[]
            for row in rows:
                if i<len(row):
                    try:
                        val=float(row[i])
                        values.append(val)
                    except (ValueError,TypeError):
                        pass
            if values:
                min_val=min(values)
                max_val=max(values)
                avg_val=sum(values)/len(values)
                summary.append(f"- {header}：范围 {min_val:.2f} ~ {max_val:.2f}，平均 {avg_val:.2f}")

        for i,header in enumerate(headers):
            value_counts={}
            for row in rows:
                if i<len(row):
                    val=str(row[i]).strip()
                    if val:
                        value_counts[val]=value_counts.get(val,0)+1
            if 1<len(value_counts)<=5:
                dist=",".join([f"{k}({v}条)"for k,v in sorted(value_counts.items(),key=lambda x: -x[1])])
                summary.append(f"- {header}分布：{dist}")
        return summary
