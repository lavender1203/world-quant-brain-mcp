# WorldQuant BRAIN 每日日报撰写工作流程

## 概述

本文档详细描述了撰写 WorldQuant BRAIN 平台每日日报的工作流程，旨在帮助秘书或助手接手此任务，确保日报内容全面、准确，并为用户提供有价值的见解和建议。工作流程包括数据收集、分析和报告撰写的具体步骤，以及使用的 BRAIN MCP 工具。

## 总体工作流程
0. 获取当前时间，running get_ny_time.py。
1. **认证与准备**：使用用户提供的登录凭据，通过 BRAIN MCP 工具认证，访问平台数据。
2. **数据收集**：获取用户的 收入、 alpha 数据、比赛信息、平台消息和事件等。偏好并行调用工具以提高效率。
3. **数据分析**：分析 alpha 性能、比赛规则、pyramid 分布和策略建议，包括相关性检查和年度统计。
4. **报告撰写**：按照预定义结构撰写日报，填充真实数据并提供建议。包括执行摘要，并将 Alpha 部分移到报告后部。
5. **修订与更新**：根据用户反馈或新数据更新报告内容，撰写并输出相应markdown日报文件。
6. **文档记录**：记录并更新工作流程以便他人参考。

## 具体步骤与章节对应

### 0. 执行摘要 (新增)
- **步骤**：
  1. 基于所有收集数据，总结关键洞见、机会、风险和行动优先级。
  2. 使用量化指标（如 Sharpe 提升估算）提供决策支持。
- **使用的 MCP 工具**：无，直接基于后续分析。

### 1. 日报基本信息
- **步骤**：
  1. 确定报告日期，通常是当前日期（如 2025年8月9日）。使用系统日期动态获取。
  2. 填写报告人和收件人信息，通常是秘书（AI 助手）和用户姓名。
- **使用的 MCP 工具**：无，直接手动输入或通过简单脚本获取日期。

### 2. 平台动向 (调整顺序)
- **步骤**：
  1. **获取平台更新**：获取 BRAIN 平台最近的公告和更新。
     - 使用工具：`mcp_brain-api_get_messages`（设置 `limit` 为 null，`offset` 为 0）。
  2. **社区动态**：从消息中提取社区相关信息，如研究论文或热门话题。
  3. **排行榜变化**：记录用户位置变化。
     - 使用工具：`mcp_brain-api_get_leaderboard`（设置 `user_id` 为用户 ID，如 "CQ89422"）。
   4. **多样性分数**：收集用户最近一个季度的多样性分数，获知其value factor趋势，该分数捕捉用户提交Alpha的多样性，来判断其value factor的变化趋势，在0-1之间，越高越好，据此提出具体建议。
- **使用的 MCP 工具**：
  - `mcp_brain-api_get_messages`：获取平台公告和社区动态。
  - `mcp_brain-api_get_leaderboard`：获取用户排行榜统计。
  - `mcp_brain-api_value_factor_trendScore`：用户value factor趋势，又名多样性分数。

### 3. 比赛参与与进度
- **步骤**：
  1. **获取用户参与的比赛**：获取用户当前参与的所有比赛信息。
     - 使用工具：`mcp_brain-api_get_user_competitions`（设置 `user_id` 为 "self"）。
  2. **筛选未截止比赛**：根据比赛日期判断哪些比赛尚未截止，优先关注这些比赛。
  3. **比赛进度报告**：记录用户在每个比赛中的排名、提交的 alpha 表现等信息。
  4. **⚠️ 关键：比赛规则与要求详细分析**：获取每个比赛的详细规则和要求。
     - 使用工具：`mcp_brain-api_get_competition_details` 和 `mcp_brain-api_get_competition_agreement`（设置 `competition_id` 为具体比赛 ID）。
     - **必须仔细阅读比赛协议**：特别注意universe要求、delay要求、Alpha类型限制等关键参数。
     - **常见错误**：例如GAC类比赛要求GLOBAL universe，而非特定region（如USA）。
  5. **比赛相关计划与建议**：基于规则和用户当前表现，提供下一步行动建议和研究方向。
     - **验证符合性**：确保推荐的Alpha完全符合比赛规则要求。
     - **结合 pyramid 缺失类别**：在符合比赛规则的前提下，考虑pyramid优化。
- **使用的 MCP 工具**：
  - `mcp_brain-api_get_user_competitions`：获取用户参与的比赛列表。
  - `mcp_brain-api_get_competition_details`：获取比赛详细信息。
  - `mcp_brain-api_get_competition_agreement`：获取比赛规则和条款。

### 4. 未来活动预告
- **步骤**：
  1. **获取即将到来的事件**：获取 BRAIN 平台上的比赛、研讨会或其他活动信息，过滤过去事件（基于当前日期，如 2025-08-09）。
     - 使用工具：`mcp_brain-api_get_events`（设置 `random_string` 为任意值，如 "dummy"）。
  2. **计划任务**：基于当前 alpha 和比赛状态，列出未来几天计划完成的任务。
- **使用的 MCP 工具**：
  - `mcp_brain-api_get_events`：获取平台事件信息。

### 5. 研究回归与建议
- **步骤**：
  1. **研究回归**：基于当前 alpha 表现总结研究成果，包括年度统计。
  2. **建议**：综合 alpha 表现、比赛要求和平台动向，提供 alpha 优化、比赛策略、数据字段探索和风险管理等方面的建议。优先级列表化。
- **使用的 MCP 工具**：基于 Alpha 部分数据。

### 6. Alpha 进展与状态 (移到后部)
- **步骤**：
  1. **获取 IS (In-Sample) Alpha 数据**：获取用户当前正在回测的 alpha 信息。
     - 使用工具：`mcp_brain-api_get_user_alphas`（设置 `stage` 为 "IS"，`limit` 为 30，`offset` 为 0）。
  2. **获取 OS (Out-of-Sample) Alpha 数据**：获取用户最近成功提交的 alpha 信息。
     - 使用工具：`mcp_brain-api_get_user_alphas`（设置 `stage` 为 "OS"，`limit` 为 30，`offset` 为 0）。
  3. **昨日进展**：查看平台日志或使用 `mcp_brain-api_get_user_activities` 追踪活动。
  4. **性能分析**：分析每个 alpha 的关键指标（如 Sharpe Ratio、PnL、Fitness），与平台标准对比。并行调用工具获取细节。
     - 使用工具：`mcp_brain-api_get_alpha_details`、`mcp_brain-api_analyze_alpha_performance`、`mcp_brain-api_get_alpha_pnl`、`mcp_brain-api_get_alpha_yearly_stats`、`mcp_brain-api_check_correlation` (阈值 0.7)。
  5. **OS Alpha 详细分析**：对每个 OS alpha 分析数据字段、运算符和含义。提供两个角度改进建议：(1) Idea 本身 (e.g., 修改窗口、添加运算符)；(2) 结合比赛 (e.g., GAC2025 要求) 或近季度缺失 pyramid (使用 `mcp_brain-api_get_pyramid_alphas` 和 `mcp_brain-api_get_pyramid_multipliers`，推荐具体数据字段)。
  6. **其他数据字段建议**：基于策略，使用 `mcp_brain-api_get_datafields` 搜索并推荐字段 (e.g., search="EPS")。
- **使用的 MCP 工具**：
  - `mcp_brain-api_get_user_alphas`：获取 IS/OS 列表。
  - `mcp_brain-api_get_alpha_details`：详细代码/描述。
  - `mcp_brain-api_analyze_alpha_performance`：全面性能分析。
  - `mcp_brain-api_check_correlation`：相关性检查。
  - `mcp_brain-api_get_alpha_pnl`：PnL 数据。
  - `mcp_brain-api_get_alpha_yearly_stats`：年度统计。
  - `mcp_brain-api_get_pyramid_alphas` 和 `mcp_brain-api_get_pyramid_multipliers`：pyramid 分布和乘数。
  - `mcp_brain-api_get_datafields`：推荐数据字段。

## 其他注意事项

- **认证**：在开始任何数据获取之前，需使用 `mcp_brain-api_authenticate` 工具进行认证，提供用户的电子邮件和密码。
- **动态日期**：使用系统日期动态获取当前日期，确保事件过滤准确（e.g., 排除过去事件）。
- **并行工具调用**：优先并行调用 MCP 工具以加速数据收集。
- **善用论坛**：善用论坛，获取更多信息。
- **用户反馈**：在每个阶段完成后，检查用户是否有补充信息或修改意见，并相应更新报告。
- **任务管理**：使用 `todo_write` 工具创建和更新待办事项列表，确保每个步骤按部就班完成。

## 质量控制与错误防范

### 常见错误及防范措施
1. **比赛规则理解错误**：
   - **错误示例**：误认为GAC2025接受USA region Alpha，实际要求GLOBAL universe
   - **防范措施**：必须详细阅读`mcp_brain-api_get_competition_agreement`返回的完整规则文档
   - **验证步骤**：在提供建议前，再次确认Alpha的universe、delay等参数符合比赛要求

2. **数据解读错误**：
   - **防范措施**：对关键指标进行交叉验证，如Sharpe ratio、fitness等
   - **质量检查**：确保所有建议都有数据支撑，避免主观推测

3. **输出格式错误**：
   - **用户偏好**：根据用户要求选择聊天输出或markdown文件
   - **结构完整性**：确保日报包含所有必需章节且逻辑清晰

### 持续改进机制
- 记录每次错误的根本原因
- 更新工作流程以防止类似错误重复发生
- 建立验证清单确保关键信息准确性

## 总结

以上工作流程涵盖了撰写 BRAIN 平台每日日报的各个方面，从数据收集到报告撰写和更新。通过使用指定的 MCP 工具，秘书可以获取必要的数据并分析用户在平台上的表现，从而提供有针对性的建议和见解。如有任何问题或需要进一步指导，请随时与前任秘书或平台支持团队联系。
