# SillyTavern 提示词构建 / 预设 / 宏引擎 技术分析报告

分析对象：`E:\研究生\LLM\game-agent\.st-inspect`，commit `06bde93`（2026-09-14），只读分析，未修改任何文件。

---

## 1. Prompt Manager 模型与最终消息数组装配

### 1.1 Prompt 对象 schema

`Prompt` 类定义于 `public/scripts/PromptManager.js:80-199`，字段（含 JSDoc）：

| 字段 | 含义 | 关键行 |
|---|---|---|
| `identifier` | 唯一 ID（`main`/`nsfw`/`jailbreak`/`chatHistory`…） | :88-91 |
| `name` | 显示名 | :105-109 |
| `role` | `system` / `user` / `assistant` | :93-97 |
| `content` | 内容模板（允许宏） | :99-103 |
| `system_prompt` | 是否属"系统提示词"（与用户自定义提示词区分） | :111-115 |
| `marker` | 是否为占位标记（无 content，由运行时填充） | :159-163 |
| `injection_position` | `INJECTION_POSITION.RELATIVE=0` / `ABSOLUTE=1` | :37-40, :123-127 |
| `injection_depth` | 绝对注入深度（默认 `DEFAULT_DEPTH=4`） | :31, :129-133 |
| `injection_order` | 同深度内的排序（默认 `DEFAULT_ORDER=100`） | :32, :135-139 |
| `injection_trigger` | 仅在指定 generation type 下生效的字符串数组 | :153-157 |
| `position` | 相对插槽（`'start'`/`'end'`，由扩展提示词使用） | :117-121 |
| `forbid_overrides` | 禁止角色卡覆盖 | :141-145 |
| `extension` | 由扩展注册 | :147-151 |

注意 `enabled` **不在 `Prompt` 上**，而在 `prompt_order` 条目里（见下）。

### 1.2 标识符清单

`chatCompletionDefaultPrompts`（`PromptManager.js:2001-2081`）与 `default/content/presets/openai/Default.json:51-129` 一致：`main`（Main Prompt）、`nsfw`（Auxiliary Prompt）、`dialogueExamples`（marker）、`jailbreak`（Post-History Instructions）、`chatHistory`（marker）、`worldInfoAfter`（marker）、`worldInfoBefore`（marker）、`enhanceDefinitions`、`charDescription`（marker）、`charPersonality`（marker）、`scenario`（marker）、`personaDescription`（marker）。

运行期还会额外塞入：`impersonate`、`quietPrompt`、`groupNudge`、`bias`、`continueNudge`（`openai.js:1374-1386, 908-925`），扩展提示词 `summary`、`authorsNote`、`vectorsMemory`、`vectorsDataBank`、`smartContext`、`personaDescription`（`openai.js:1389-1435`）。

### 1.3 `prompt_order`

`PromptManager` 配置为 `{ promptOrder: { strategy: 'global', dummyId: 100000 } }`（`PromptManager.js:334-336`）。`prompt_order` 是**按 `character_id` 分组的数组**：

```json
"prompt_order": [{ "character_id": 100000, "order": [{ "identifier": "main", "enabled": true }, ...] }]
```
（`Default.json:130-233`；100000=单人全局，100001=群聊。`promptManagerDefaultPromptOrder` 见 `PromptManager.js:2087-2136`。）

`getPromptCollection(generationType)`（`PromptManager.js:1516-1541`）按 order 顺序遍历：`entry.enabled && shouldTrigger(prompt, generationType)` 才加入；`main` 即使被禁用也会以空内容保留，供扩展做相对插入（:1531-1537）。每个 prompt 经 `preparePrompt()`（:1277-1290）做宏替换（`substituteParams`，群聊时传 `groupOverride`）。

### 1.4 最终数组装配算法（`openai.js`）

**步骤 A — 合并系统提示词与用户提示词**（`preparePromptsForChatCompletion`, `openai.js:1367-1516`）：
构造 `systemPrompts` 数组（worldInfoBefore/After、charDescription、charPersonality、scenario、impersonate、quietPrompt、groupNudge、bias + 扩展提示词，:1374-1467）；再取用户定义的 `prompts`（:1470），逐条把 `systemPrompts` 合并进去——若 prompt manager 中已有同 identifier，则用其 `injection_position/depth/order/role` 覆盖（:1477-1486），否则追加（:1492）。随后应用角色卡覆盖：`systemPromptOverride` → `main`，`jailbreakPromptOverride` → `jailbreak`，但 `forbid_overrides === true` 或被禁用时跳过（:1496-1513）。

**步骤 B — 组装消息集合**（`populateChatCompletion`, `openai.js:1185-1347`）：
`ChatCompletion.messages.collection` 是**按 prompt index 定位的槽位数组**（`add(collection, position)` 用 `collection[position] = collection`，`openai.js:3998-4013`）。执行顺序：

1. 固定顺序加入：`worldInfoBefore` → `main` → `worldInfoAfter` → `charDescription` → `charPersonality` → `scenario` → `personaDescription`（:1212-1218）；
2. 抽出 `absolutePrompts`（`injection_position === ABSOLUTE`）与 `userRelativePrompts`（:1242-1253）；
3. 加入 `['nsfw','jailbreak']` + 全部相对用户提示词（:1241, :1255-1257）；
4. `enhanceDefinitions`（:1260）、`bias`（:1263）；
5. 相对扩展提示词（`summary`/`authorsNote`/…）通过 `injectToMain(prompt, 'start'|'end')` 插入 `main` 集合内部；若 `main` 本身是绝对注入，则改写成同深度/同 order 的绝对提示词（:1265-1307）；
6. 工具定义预算预留（:1310-1316）；continue 预填充（:1320-1331）；
7. **聊天内注入**：`messages = await populationInjectionPrompts(absolutePrompts, messages)`（:1334）；
8. 按 `power_user.pin_examples` 决定先放 examples 还是先放 chatHistory（:1337-1343）；
9. 控制提示词（impersonate / quiet / continue prefill）**永远最后**追加（:1345-1346）。

**步骤 C — 在聊天内注入**（`populationInjectionPrompts`, `openai.js:810-875`）：
```js
for (let i = 0; i <= maxDepth; i++) {
    const depthPrompts = prompts.filter(prompt => prompt.injection_depth === i && prompt.content);
    ...
    const orders = Object.keys(orderGroups).sort((a, b) => +b - +a);   // :842 优先级数字小的更靠后
    for (const order of orders) { for (const role of ['system','user','assistant']) { ... } }
    const injectIdx = i + totalInsertedMessages;
    messages.splice(injectIdx, 0, ...roleMessages);                     // :867-868
}
messages = messages.reverse();                                          // :873
```
关键点：`messages` 此时是**最新在前**（由 `setOpenAIMessages` 的倒序循环构造，`openai.js:578`, 调用点 `script.js:4834`），因此 `splice(i)` 表示"距末尾 i 条"；函数末尾 `reverse()` 把它变回时间正序。`injection_order` **数值越小越靠后**（:842 的 `+b - +a`）。同一深度内按 `system → user → assistant` 分组拼接（:847-863）。深度上限为 `MAX_INJECTION_DEPTH = 10000`（`script.js:500`, `getExtensionPromptMaxDepth()` `script.js:3281-3288`）。

**步骤 D — 后处理**：`squash_system_messages` 合并相邻 system 消息（`openai.js:1608-1610`）；随后发 `CHAT_COMPLETION_PROMPT_READY` 事件（:1619），允许扩展改写最终数组。ChatML 数组最终由 `src/prompt-converters.js` 转成各家格式（`convertClaudeMessages`:197、`convertGooglePrompt`:432、`convertCohereMessages`:384、`convertTextCompletionPrompt`:961 等）。

---

## 2. 宏引擎

ST 目前有**两套并存**的宏实现，由 `power_user.experimental_macro_engine` 切换（`script.js:2997-2999`）。`public/scripts/macros.js` 是旧引擎（`evaluateMacros`, `macros.js:610-715`）并已把 `MacrosParser` 标记为 `@deprecated`（`macros.js:38-42`）；新引擎在 `public/scripts/macros/` 下，采用 **lexer → parser(CST) → CST walker** 架构（`MacroLexer.js` / `MacroParser.js` / `MacroCstWalker.js` / `MacroRegistry.js` / `MacroEngine.js`，入口 `macro-system.js:44-85`）。

### 2.1 语法

- 基本形式 `{{name}}`、`{{name::arg}}`、`{{name::a::b}}`，也兼容空格/冒号分隔（`{{roll 1d20}}`、`{{roll::1d20}}`，`core-macros.js:303-337`）。
- **变量简写**：`.varName` 取局部变量、`$varName` 取全局变量（`MacroLexer.js:102-113, 188-191`；正则 `MACRO_VARIABLE_SHORTHAND_PATTERN = /[a-zA-Z](?:[\w\-_]*[\w])?/`，:26）。
- **作用域宏**：`{{setvar::x}}...{{/setvar}}`、`{{if 条件}}...{{else}}...{{/if}}`，闭合标记 `/` 由 `MacroFlagType.CLOSING_BLOCK` 解析（`MacroFlags.js:56-63`），`{{if}}` 使用 `delayArgResolution: true` 只解析被选中的分支（`core-macros.js:158-209`）。
- **注释**：`{{// 任意文本}}` → `''`（`core-macros.js:282-299`），也可作用域化 `{{//}}...{{///}}`。
- **转义**：`\{` / `\}` 在 post-processor 中被还原（`MacroEngine.js:305-308`）；`\{\{` 因不匹配 `Macro.Start` 而作为纯文本通过（同处注释）。宏参数中 `\,` 用于在 `{{random}}` 里转义逗号（`core-macros.js:419-421`）。

### 2.2 标志（flags）系统

定义于 `public/scripts/macros/engine/MacroFlags.js:27-77`，位于 `{{` 与名称之间，可组合：

| 符号 | 名称 | 状态 |
|---|---|---|
| `!` | IMMEDIATE 先解析 | **未实现**（:94-100） |
| `?` | DELAYED 后解析 | **未实现**（:101-107） |
| `~` | REEVALUATE | **未实现**（:108-114） |
| `>` | FILTER 管道过滤器 | 已解析未实现（:115-121） |
| `/` | CLOSING_BLOCK 作用域闭合 | 已实现（:122-128） |
| `#` | PRESERVE_WHITESPACE 保留作用域空白 | 已实现，兼容旧 Handlebars `{{#if}}`（:129-135） |

用户提到的"`{{macro}}` 的 `#` 标志"实际就是 `PRESERVE_WHITESPACE`，并非"原样输出"。

### 2.3 展开顺序与递归

`MacroEngine.evaluate(input, env, {contextOffset})`（`MacroEngine.js:117-159`）：
1. **preProcessors**（按 priority 升序）：`{{time_UTC-10}}` → `{{time::UTC-10}}`（:278-281）、`<USER>/<BOT>/<CHAR>/<GROUP>` → `{{user}}/{{char}}/{{group}}`（:285-293）。
2. `MacroParser.parseDocument()` 得到 CST；lexing/parser 错误只告警不中断（:125-139）。
3. `MacroCstWalker.evaluateDocument()` **自内向外**求值：先解析嵌套宏与作用域内容，再调用 `#resolveMacro`（`MacroCstWalker.js:370-509, 802-871`）。嵌套内容重解析时用 `contextOffset` 保留原文绝对偏移，使 `{{pick}}` 这类按位置播种的宏在不同嵌套层级仍稳定（:44-54, :892, :1160）。
4. `#resolveMacro` 优先查 `env.dynamicMacros`（大小写不敏感），未注册的宏**原样保留** `{{name}}`（`MacroEngine.js:173-217`）。
5. **postProcessors**：反转义 `\{`→`{`（priority 10）、`{{trim}}` 与周边换行删除（20）、清理残留 `ELSE_MARKER`（30）（:299-323）。

所有注册点由 `initRegisterMacros()` 依序注册 core → env → state → chat → time → variable → instruct（`macro-system.js:66-85`）。

### 2.4 宏清单与实现位置

- **身份/环境**：`{{user}}`、`{{char}}`、`{{group}}`（隐藏别名 `charIfNotGroup`）、`{{groupNotMuted}}`、`{{notChar}}`、`{{model}}`、`{{original}}`、`{{isMobile}}`（`definitions/env-macros.js:16-205`）。
- **角色卡**：`{{charPrompt}}`、`{{charInstruction}}`、`{{charDescription}}`、`{{charPersonality}}`、`{{charScenario}}`、`{{persona}}`、`{{mesExamples}}`/`{{mesExamplesRaw}}`、`{{charDepthPrompt}}`、`{{charCreatorNotes}}`、`{{charFirstMessage}}`（支持 `::index` 取备用开场白，:143-167）、`{{charVersion}}`（:168）。
- **聊天事实**：`{{lastMessage}}`、`{{lastMessageId}}`、`{{lastUserMessage}}`、`{{lastCharMessage}}`、`{{firstIncludedMessageId}}`、`{{firstDisplayedMessageId}}`、`{{lastSwipeId}}`、`{{currentSwipeId}}`、`{{allChatRange}}`（`definitions/chat-macros.js:9-100`）。
- **时间**：`{{time}}`（默认 `moment().format('LT')`）、`{{date}}`（`LL`）、`{{weekday}}`、`{{isotime}}`、`{{isodate}}`、`{{datetimeformat 格式}}`、`{{idleDuration}}`、`{{timeDiff::a::b}}`（`definitions/time-macros.js:11-132`；旧实现 `macros.js:660-669`）。
- **随机**：`{{roll 1d20}}`（droll，纯数字按 `1dX`，`core-macros.js:303-337`）、`{{random::a::b}}`（每次重掷，`seedrandom('added entropy.', {entropy:true})`，:340-360）、`{{pick::a::b}}`（按 `chatIdHash + contentHash + globalOffset + pick_reroll_seed` 播种，同一位置结果稳定，:363-407）。
- **工具/控制**：`{{input}}`（读 `#send_textarea` 的当前值，`core-macros.js:228-233`）、`{{maxPrompt}}/{{maxContext}}/{{maxResponse}}`（:236-265）、`{{reverse::x}}`（:266）、`{{noop}}`/`{{space}}`/`{{newline}}`/`{{trim}}`（:32-133）、`{{outlet::key}}`（取世界书 outlet 内容，:450-468）、`{{banned::word}}`（旧式 `{{banned "word"}}`；加入 textgen 禁用词表并返回空串，:425-447）。
- **运行时状态**：`{{lastGenerationType}}`、`{{hasExtension::name}}`（`definitions/state-macros.js:35-50`）。
- **变量宏**：见 2.5。
- **Instruct 宏**：`{{instructInput}}`、`{{instructOutput}}`、`{{defaultSystemPrompt}}` 等，值来自 `power_user.instruct.*`（`definitions/instruct-macros.js:37-74`）。

**`{{summary}}` 不是内建宏**：它由记忆扩展注册——`public/scripts/extensions/memory/index.js:1120-1129`（`macros.register('summary', {...})` 与旧 API 双注册）。若该扩展被禁用，`{{summary}}` 会原样保留。

### 2.5 变量存储与作用域

`public/scripts/variables.js`：
- **局部变量**：`chat_metadata.variables[name]`（`getLocalVariable` :22-46，`setLocalVariable` :48-81），即**按聊天文件持久化**；`setLocalVariable` 结束调用 `saveMetadataDebounced()`（:79）。
- **全局变量**：`extension_settings.variables.global[name]`（`getGlobalVariable` :83-103，`setGlobalVariable` :105-134），即随用户设置持久化。
- 两者都做了"能转数字就转数字"的隐式类型推断（:45, :102），并用 `JSON.parse/stringify` 支持数组/对象下标（`setvarkey`/`getvarkey`）。
- `resolveVariable(name, scope)` 的查找优先级：斜杠命令作用域 → 局部 → 全局 → 原样返回（:218-232）。
- 宏名清单（新引擎）：`setvar/addvar/incvar/decvar/getvar/hasvar/deletevar/setvarkey/getvarkey` + 对应的 `*globalvar*`（`definitions/variable-macros.js:11-417`），别名 `varexists`/`flushvar`/`setvarindex` 等。旧引擎的正则版在 `variables.js:238-261`。

---

## 3. 预设系统

### 3.1 前端 `PresetManager`

`class PresetManager { constructor(select, apiId) }`（`preset-manager.js:111-115`）——**没有 prefix/containerElement 参数**。实例由 `registerPresetManagers()` 扫描 DOM 上的 `select[data-preset-manager-for]` 自动创建（:102-108），因为同一套类要服务 8 种后端。`getPresetManager(apiId)`（:83-96）把 `koboldhorde` 归一为 `kobold`，未注册返回 `null`。

主要方法：`savePreset`（:473-505）、`deletePreset`（:785-823）、`renamePreset`（:511-524，实为"存新删旧"）、`getPresetSettings`（:647-750，按白名单剥离字段）、`getDefaultPreset`（:830-844）。导出为浏览器文件下载（:1085-1099），导入为 `parseJsonFile` → `savePreset(name, data)`（:1121-1126），**只校验 JSON 可解析，无 schema 校验**；另有 `masterSections` 批量导入导出（:117-207, :244-335）。

### 3.2 后端 `/api/presets`

`src/endpoints/presets.js` 三条路由，目录映射见 `getPresetSettingsByAPI`（:16-38）：`kobold|koboldhorde→koboldAI_Settings`、`novel→novelAI_Settings`、`textgenerationwebui→textGen_Settings`、`openai→openAI_Settings`、`instruct/context/sysprompt/reasoning→同名目录`。

```js
router.post('/save', ...)    // presets.js:42-58   body {preset, name, apiId} → {name}
router.post('/delete', ...)  // presets.js:60-81   body {name, apiId} → 200/404
router.post('/restore', ...) // presets.js:83-103  body {name, apiId} → {isDefault, preset}
```
写入用 `writeFileAtomicSync(fullpath, JSON.stringify(preset, null, 4))`（:56），文件名经 `sanitize-filename`（:43）。

### 3.3 预设类别

`default/content/presets/` 下：`openai`(聊天补全)、`textgen`、`kobold`、`novel`、`instruct`(指令模板)、`context`(故事串模板)、`sysprompt`(系统提示词)、`reasoning`(思维链模板)、`quick-replies`、`moving-ui`。前 8 类走 `PresetManager`；后两类随 `/api/settings/get` 下发，不受其管理。

### 3.4 一个 OpenAI 预设包含什么

`default/content/presets/openai/Default.json`（245 行）可归为四组：
- **连接/模型**：`chat_completion_source`、`reverse_proxy`、`proxy_password`、`custom_url`/`custom_include_body`/…、各厂商 `*_model`；
- **采样参数**：`temperature`、`frequency_penalty`、`presence_penalty`、`top_p`、`top_k`、`top_a`、`min_p`、`repetition_penalty`、`openai_max_context`、`openai_max_tokens`、`seed`、`n`、`stream_openai`；
- **提示词结构**：`prompts`（:51-129）、`prompt_order`（:130-233）、`use_sysprompt`、`squash_system_messages`、`names_behavior`，以及固定串 `impersonation_prompt`/`new_chat_prompt`/`continue_nudge_prompt`/`group_nudge_prompt`/`wi_format`/`scenario_format`/`personality_format`/`bias_preset_selected`；
- **工具**：只有行为开关 `function_calling`、`tool_call_recurse_limit`、`tool_reasoning_mode`、`show_thoughts`、`reasoning_effort`（`openai.js:392-397`）。**`tool_rp_*` 字段在本 checkout 中不存在**（全仓 grep 无匹配）；function 定义本身由扩展通过 `ToolManager.registerFunctionTool` 注册（`tool-calling.js:269`），不落在预设 JSON 里。

### 3.5 instruct 模板 schema

以 `default/content/presets/instruct/Alpaca.json` 为准（24 字段）：`input_sequence`、`output_sequence`、`last_output_sequence`、`system_sequence`、`last_system_sequence`、`stop_sequence`、`first_output_sequence`、`first_input_sequence`、`last_input_sequence`、`input_suffix`、`output_suffix`、`system_suffix`、`story_string_prefix`、`story_string_suffix`、`wrap`、`macro`、`names_behavior`、`activation_regex`、`skip_examples`、`sequences_as_stop_strings`、`user_alignment_message`、`system_same_as_user`、`name`。**没有 `system_prompt` 字段**（已迁到 sysprompt，`sysprompt.js:80-95`）；`getInstructStoryString` / `createExampleMessage` 在本版本**未找到**，对应实现是 `formatInstructModeStoryString`（`instruct-mode.js:478-502`）与 `formatInstructModeExamples`（:511-579）。

`names_behavior` 枚举为字符串 `{NONE:'none', FORCE:'force', ALWAYS:'always'}`（:17-21；注意与 OpenAI 预设里的数值枚举 `NONE:-1/DEFAULT:0/COMPLETION:1/CONTENT:2`（`openai.js:206-211`）**不是一回事**）。`macro` 为 true 时序列中的 ST 宏与 `{{name}}` 会被替换（:438-444），false 时按字面输出。

### 3.6 共享方式

预设是**用户数据目录下的磁盘 JSON 文件**，由 `/api/settings/get` 全量下发前端；**角色卡不内嵌预设**。"绑定角色"只是命名约定（`savePresetAs` 提示 + `autoSelectPreset` 按名匹配，`preset-manager.js:49-76, 452`）。

---

## 4. 推理 / 思维链支持

### 4.1 模板 schema

`ReasoningTemplate` 只有 4 个字段：`name` / `prefix` / `suffix` / `separator`（`reasoning.js:21-27`）。默认模板 `Think XML`（:34），运行时值存 `power_user.reasoning = { name, auto_parse:false, add_to_prompts:false, auto_expand:false, show_hidden:false, prefix:'<think>', suffix:'</think>', separator:'\n', max_additions:1 }`（`power-user.js:274-284`）。预设文件即纯模板，例如：

```json
{ "name": "DeepSeek", "prefix": "<think>\n", "suffix": "\n</think>", "separator": "\n\n" }
```
（`default/content/presets/reasoning/DeepSeek.json`；另有 Blank/Think XML/OpenAI Harmony/Gemma 4。）

### 4.2 解析与剥离

- `extractReasoningFromData(data, opts)` 从各家 API 响应里取结构化推理（`reasoning.js:112-168`）：DeepSeek/XAI 取 `choices[0].message.reasoning_content`，Claude 取 `content.filter(p => p.type === 'thinking')`，Gemini 取 `parts.filter(p => p.thought)`。
- `parseReasoningFromString(str, {strict}, template)` 从**纯文本**里剥离思维链（:1461-1490）。**实现是正则替换而非索引扫描**：
```js
if (!template.prefix || !template.suffix) return null;                 // :1465-1467
const regex = new RegExp(`${(strict ? '^\\s*?' : '')}${escapeRegex(template.prefix)}(.*?)${escapeRegex(template.suffix)}`, 's');
```
即"前缀/后缀任一为空就不解析"——这就是"空白模板即关闭"的机制。`formatReasoning` 是其逆操作（:1503-1521）。流式路径确实用 `startsWith/slice/indexOf` 做增量解析（:500-521），由 `ReasoningHandler` 驱动（:276, :414, :454, :538）。
- **没有从模型输出自动推断 prefix/suffix 的逻辑**（未找到）；`auto_parse` 只是"是否对历史消息自动回填解析"的开关，默认 false（`power-user.js:276`，门控 `reasoning.js:481-484`）。
- 解析结果写 `message.extra.reasoning` / `extra.reasoning_type` / `extra.reasoning_duration` / `extra.reasoning_signature`（`reasoning.js:1595-1596, 436`；`script.js:3791`）。**没有 `mes.reasoning` 顶层字段**。

### 4.3 回注到后续回合

两条通道：
1. **拼回正文**（默认关闭）：`script.js:4531-4557` 倒序遍历 `coreChat`，调用 `promptReasoning.addToMessage(mes, getRegexedString(extra.reasoning, regex_placement.REASONING, {isPrompt:true, depth}), isPrefix, duration)`，格式为 `前缀 + 推理 + 后缀 + 分隔符 + 正文`（`reasoning.js:777`）。受 `add_to_prompts`（默认 false）与 `max_additions`（默认 1）限制（:746, :729-733），群聊中跳过其他角色（`script.js:4537-4542`）。
2. **结构化字段**：`openai.js:630, 644` 把 `extra.reasoning` 与 `extra.reasoning_signature` 写入出站 message 对象，仅当 `api`/`model` 与当前生成一致才带上（:624-630）。

### 4.4 请求体与流式

`openai.js:2820-2821` 写入 `'include_reasoning': Boolean(settings.show_thoughts)` 与 `'reasoning_effort': getReasoningEffort(settings, model)`；`getReasoningEffort()`（:2545-2663）把 `auto/min/max` 映射成各厂商可接受的值并按模型能力裁剪，不支持的源会被 `delete`（:2836, :2966, :3115-3118）。流式抽取在 `getStreamingReply()`（:3222-3300），累积 `delta.thinking` / `delta.reasoning_content`。

---

## 5. Regex / 脚本变换

位置：`public/scripts/extensions/regex/`，`manifest.json` 里 `"hooks": { "activate": "init" }`，`loading_order: 1`（最早加载，保证其他模块可以 import `engine.js`）。

### 5.1 脚本 schema

`RegexScriptData`（`public/scripts/char-data.js:88-101`）+ 运行时补充：

`id`（UUID，缺省时由 `index.js:517-519` 补全）、`scriptName`、`findRegex`、`replaceString`、`trimStrings[]`、`placement[]`、`disabled`、`markdownOnly`、`promptOnly`、`runOnEdit`、`substituteRegex`、`minDepth`、`maxDepth`。

**未找到** `isPreset`、`macroFlags` 或任何脚本级 `flags` 字段——JS 正则 flags 只写在 `findRegex` 字面量内部。

`regex_placement` 枚举（`engine.js:281-292`）：
```js
MD_DISPLAY: 0,   // @deprecated
USER_INPUT: 1, AI_OUTPUT: 2, SLASH_COMMAND: 3,
// 4 - sendAs (legacy)
WORLD_INFO: 5, REASONING: 6,
```
`substitute_regex` 枚举（:298-302）：`NONE:0`（findRegex 原样）、`RAW:1`（先做宏替换）、`ESCAPED:2`（宏替换后按正则元字符转义，`sanitizeRegexMacro` :304-323）。

### 5.2 执行入口与顺序

`getRegexedString(rawString, placement, {characterOverride, isMarkdown, isPrompt, isEdit, depth})`（`engine.js:334-381`）是唯一入口，脚本顺序为 `GLOBAL(0) → SCOPED(1) → PRESET(2)`（`SCRIPT_TYPES` :11-16 与 `getRegexScripts` :98-100）。

三条过滤条件：
- `(script.markdownOnly && isMarkdown) || (script.promptOnly && isPrompt) || (!markdownOnly && !promptOnly && !isMarkdown && !isPrompt)`（:348-355）——**`isPrompt`/`isMarkdown` 决定该脚本在"发给模型"还是"显示给用户"时生效**；
- `isEdit && !script.runOnEdit` 跳过（:356-359）；
- `depth` 在 `[minDepth, maxDepth]` 之外跳过（`minDepth >= -1`、`maxDepth >= 0` 才生效，:362-372）；
- 最后 `script.placement.includes(placement)`（:374）。

`runRegexScript`（:391-465）用 LRU 缓存编译正则（`RegexProvider` :40-90），`replaceString` 支持 `{{match}}`、`$1`/`$<name>` 捕获组（:421-435），并对每个捕获内容应用 `trimStrings`。**未找到 `{{group1}}` 语法。**

**flags 解析**：`/pattern/flags` 字面量由 `regexFromString`（`public/scripts/utils.js:1387-1402`）解析，白名单为 `g m i x X s u U A J` 且**不允许重复**：
```js
var m = input.match(/(\/?)(.+)\1([a-z]*)/i);
if (m[3] && !/^(?!.*?(.).*?\1)[gmixXsuUAJ]+$/.test(m[3])) { return RegExp(input); }
return new RegExp(m[2], m[3]);
```
`y` 等不在白名单的 flag 会被静默降级为字面量匹配；模式的非法语法抛错被 `catch` 吞掉并返回原串（`engine.js:414-416`）。编辑器只提示 `g` 与 `i`（`index.js:1362-1363`）。因此"正则 flags"在 ST 里是**字面量内嵌**，而不是独立的配置字段。

**`replaceString` 的四步处理**（`engine.js:419-445`）：① `{{match}}`（大小写不敏感）→ `$0`（:421）；② 展开 `$n` / `$<name>`（:422-441）；③ 每个插入值先过 `filterString(..., trimStrings)`，其中 `trimStrings` 自身也做宏替换（:438, :457-464）；④ 整体再做一次 `substituteParams`（:444）——即**宏替换发生两次**（findRegex 侧由 `substituteRegex` 控制，replaceString 侧无条件）。

### 5.3 管线中的调用点

| 场景 | placement | 位置 |
|---|---|---|
| 用户输入 | `USER_INPUT` | `script.js:5875`, `script.js:4503-4506`（`isPrompt:true` + `depth`） |
| AI 输出（发给模型） | `AI_OUTPUT` | `script.js:4503-4506`, `script.js:6481` |
| AI 输出（显示） | `AI_OUTPUT` | `script.js:1860`（`messageFormatting`，`isMarkdown:true`） |
| 世界书条目 | `WORLD_INFO` | `world-info.js:5205`（先宏后正则：`entry.content = substituteParams(...)` 在 `:5058`） |
| 思维链 | `REASONING` | `reasoning.js:429/1036/1215/1546/1595`, `script.js:4547, 5503, 1836` |
| 斜杠命令 | `SLASH_COMMAND` | `slash-commands.js:4716, 5717, 5944, 6087` |

**关键：宏在正则之前**——WI 内容先 `substituteParams`（`world-info.js:5058`）再 `getRegexedString`（:5205）；显示侧宏在 `messageFormatting` 的 message 0 分支处理（`script.js:1808`），正则紧随其后（:1860）。显示管线的 11 步顺序有完整文档注释（`script.js:1769-1787`），扩展钩子 `BEFORE_REGEX`/`AFTER_REGEX` 可插在正则前后（:1855, :1866）。

脚本来源分三层（`engine.js:98-130`）：全局 `extension_settings.regex`、角色卡内 `characters[chid].data.extensions.regex_scripts`（需用户在 `character_allowed_regex` 中放行）、预设内嵌脚本；导入导出见 `extensions/regex/index.js`。

---

## 6. 对 Python agent harness 值得借鉴的部分

**值得复用（工程价值高）**

1. **prompt 用"标识符 + 有序表 + 启用位"描述，而非硬编码字符串拼接。** `prompts[]`（内容与角色）+ `prompt_order[character_id].order[]`（顺序与开关）的分离，使同一份默认提示词可以按角色/群聊切换布局，并让 UI、扩展、持久化都只操作数据。Python 侧等价物：`list[PromptSpec]` + `dict[profile_id, list[OrderEntry]]`。
2. **"marker" 占位符机制。** `chatHistory`/`worldInfoBefore` 等条目本身无内容，只有位置；运行时由 `addToChatCompletion('worldInfoBefore')` 填充（`openai.js:1212-1218`）。这让"内容从哪来"与"内容放哪"彻底解耦，是 ST 相对多数 agent 框架最干净的设计。
3. **绝对注入 + 深度/优先级三元组。** `injection_position=ABSOLUTE` + `injection_depth` + `injection_order`（后者**数值小的更靠后**）用两个整数表达了"在对话末尾 N 条处、以什么相对顺序插入 system/user/assistant 消息"，足以覆盖"记忆注入""作者注""临时提醒"等绝大多数需求，无需为每种注入写专门代码。
4. **宏引擎的分层与"未注册宏原样保留"。** lexer→parser→CST-walker 的分层让嵌套解析、作用域宏、转义、`{{if}}` 的惰性分支求值都自然落地；`#resolveMacro` 对未知宏返回原文（`MacroEngine.js:216-217`），使得**用户模板可以安全地前向引用尚未实现的功能**。同时 `contextOffset` 让按位置播种（`{{pick}}`）在任意嵌套下稳定——这个"位置感知的确定性随机"对可复现的 agent 行为很实用。
5. **局部/全局变量的持久化分界。** 局部变量挂 `chat_metadata`（随会话走）、全局变量挂 `extension_settings`（随用户走），语义清晰、实现只有约 120 行（`variables.js:22-134`）。加上 `.var`/`$var` 简写与 `{{if}}` 的组合，就得到了一个够用的会话状态层。
6. **`script.promptOnly` / `markdownOnly` 的双通道过滤。** 同一个文本变换规则可以声明自己只作用于"发给模型的提示词"还是"显示给用户的渲染"，并且额外带 `minDepth/maxDepth` 做最近 N 条限定。这是解决"agent 输出要清理，但用户看到的要保留"这类矛盾的通用手段。
7. **推理块的"模板即配置"。** `{prefix, suffix, separator}` 四个字段足以适配 `<think>`、`<|channel|>analysis`、Gemma `<|channel>thought` 等各家格式，且"前后缀为空即不解析"是零成本的开关（`reasoning.js:1465-1467`）。
8. **原子写 + 白名单导出的预设持久化。** `write-file-atomic`（`presets.js:56`）+ `settingsToUpdate` 白名单（`openai.js:305-409`）避免了把 API key、代理密码等敏感字段写进可分享的预设文件。

**属于历史包袱 / 不建议照搬**

- **两套宏引擎长期并存**（`macros.js` 的 715 行 `evaluateMacros` 正则链 vs 新 CST 引擎），并由 `power_user.experimental_macro_engine` 开关切换，两边的行为差异需要人工对齐。Python 新项目应直接选一种（建议 CST/惰性求值那种）。
- **正则链式替换的展开顺序是隐式契约**：`preEnvMacros → envMacros → postEnvMacros`（`macros.js:694`）以及 `{{pick}}` 依赖 `rawContent` 与 offset，顺序稍变就产生回归。新引擎用显式 priority 的 pre/post-processor 解决了一部分，但 `{{trim}}` 仍不得不作为 post-processor 打补丁（`MacroEngine.js:310-316`）。
- **8 种后端共用一套 PresetManager + 一个 `/api/presets` 路由**，靠 `getPresetSettingsByAPI` 的 switch 与前端 `data-preset-manager-for` 属性分发；类别、白名单字段、目录三处需要同步，属于典型的"配置分散"。
- **过大的单文件**：`openai.js` 7300+ 行、`script.js` 12600 行，提示词装配逻辑与 UI、token 计数、错误处理混在一起（`populateChatCompletion` 一个函数就承担了 8 类插入策略）。
- **`{{summary}}` 这类"隐藏依赖"**：它不是内建宏，而是记忆扩展注册的；关掉扩展模板就静默失效。宏注册表最好能对外暴露"哪些宏由哪个模块提供"。
- `MD_DISPLAY` 已废弃但仍占枚举位 0，`sendAs` 的枚举位 4 空缺（`engine.js:281-292`）——只作历史痕迹保留。

**若只抄一件事**：抄第 2 条的 marker + `prompt_order` 模型和第 3 条的 depth/order 注入三元组，它们用极少的概念覆盖了 90% 的提示词编排需求。
