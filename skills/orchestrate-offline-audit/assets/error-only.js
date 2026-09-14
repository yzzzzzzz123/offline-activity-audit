    (() => {
      const style = document.getElementById('error-only-preview-style');
      document.head.appendChild(style);
      const archiveRecord = document.querySelector('meta[name="offline-audit-delivery-mode"]')?.content === 'static_archive'
        && new URLSearchParams(window.location.search).has('run');
      const pageMode = archiveRecord ? 'record' : document.querySelector('meta[name="offline-audit-page-mode"]')?.content || '';
      const viewAvailable = document.querySelector('meta[name="offline-audit-view-available"]')?.content || '';
      if (pageMode === 'system' || (pageMode === 'record' && viewAvailable !== 'true')) return;
      const source = document.getElementById('audit-data');
      if (!source) throw new Error('缺少核销数据');
      const data = JSON.parse(source.textContent);
      if (pageMode === 'record') {
        Object.defineProperty(globalThis, '__offlineAuditEmbeddedView', {
          value: data,
          configurable: true,
        });
      }
      document.title = '参半渠道活动核销｜核销主工作台｜错误清单';

      const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
      const lines = (value) => String(value || '').split('\n').map((line) => line.trim()).filter(Boolean);
      const lineWith = (value, prefix) => lines(value).find((line) => line.startsWith(prefix)) || '';
      const afterPrefix = (value, prefix) => {
        const line = lineWith(value, prefix);
        return line ? line.slice(prefix.length).trim() : '';
      };
      const fieldValue = (value, label) => afterPrefix(value, `${label}：`);
      const firstNumber = (value) => {
        const match = String(value || '').match(/-?\d+(?:\.\d+)?/);
        return match ? Number(match[0]) : null;
      };
      const displayNumber = (value) => Number.isFinite(value)
        ? String(Number(value.toFixed(6)))
        : '未识别';
      const confidenceLabel = (value) => ({ high: '高', medium: '中', low: '低' }[value] || '高');
      const normalizedConfidence = (value) => ['high', 'medium', 'low'].includes(value) ? value : 'high';
      const confidencePresentation = (value, explicitScore, judgmentContext = '') => {
        const fallbackLevel = normalizedConfidence(value);
        const hasExplicitScore = explicitScore !== undefined
          && explicitScore !== null
          && String(explicitScore).trim() !== ''
          && Number.isFinite(Number(explicitScore));
        const context = String(judgmentContext || '');
        const inferredScore = () => {
          let score = fallbackLevel === 'high' ? 0.9 : fallbackLevel === 'medium' ? 0.72 : 0.52;
          if (/\d+(?:\.\d+)?\s*(?:米|元|个|件|张|家|%|天)/.test(context)) score += 0.03;
          if (/唯一|明确|精确|已定位|证据闭环|(?:^|[^不未无])一致/.test(context)) score += 0.03;
          if (/MCP|地图|坐标|距离|第\s*\d+\s*(?:页|行)|合同|Excel|POS/i.test(context)) score += 0.02;
          if (/不唯一|多个候选|模糊|低置信度|待人工|无法确认|不.{0,3}一致/.test(context)) score -= 0.03;
          if (/缺失|缺少|未提交|不完整|无法识别|不可读|未识别|没有/.test(context)) score -= 0.05;
          const bounds = fallbackLevel === 'high'
            ? [0.85, 0.99]
            : fallbackLevel === 'medium'
              ? [0.6, 0.84]
              : [0.35, 0.59];
          return Math.min(bounds[1], Math.max(bounds[0], Math.round(score * 100) / 100));
        };
        const score = hasExplicitScore
          ? Math.min(1, Math.max(0, Math.round(Number(explicitScore) * 100) / 100))
          : inferredScore();
        const level = hasExplicitScore
          ? score >= 0.85 ? 'high' : score >= 0.6 ? 'medium' : 'low'
          : fallbackLevel;
        const scoreText = String(Number(score.toFixed(2)));
        const label = confidenceLabel(level);
        return {
          level,
          score,
          scoreText,
          text: `${label}：${scoreText}`,
          filterText: `${label}置信度`,
        };
      };
      const ERROR_REASON_ORDER = [
        '资料完整性', '费用类型', '特殊审批', '合同核心字段', '主体身份', '签章完整性',
        '商品对应', '销售明细', 'POS销售明细', '门店地点低置信度', '门店无法确认', '门店水印缺失或无法核对',
        '活动日期', '陈列标准', '照片复用', '现场商品知识库', '现场商品与合同', '现场照片',
        '票据材料', '结算材料', '付款凭证', '收款人与日期', '金额复算',
      ];
      const errorReasonCategory = (value) => {
        const text = String(value || '');
        const rules = [
          ['照片复用', /照片.{0,8}(?:重复|复用)|(?:重复|复用).{0,8}照片|相同图片|图片哈希/],
          ['门店水印缺失或无法核对', /门店水印缺失|水印.{0,6}(?:缺失|无法识别|不可读)|filename_only/i],
          ['门店地点低置信度', /门店地点|水印地点|合同门店|同址|临近|距离|商场|location_unverified/i],
          ['陈列标准', /陈列标准|堆头标准|陈列.{0,8}(?:不符|未达|不足)|摆放|尺寸/],
          ['活动日期', /活动日期|活动期|执行期|合同周期|日期水印/],
          ['现场商品与合同', /现场商品.{0,8}合同|合同商品范围/],
          ['现场商品知识库', /现场商品(?:.{0,8}(?:知识库|识别))|知识库图片视觉/],
          ['POS销售明细', /POS/i],
          ['销售明细', /销售Excel|销售明细|出库|送货|电子表|配送/i],
          ['金额复算', /金额|单价|合计|奖励|申报|支持金额|差额|复算|计算|预算/],
          ['付款凭证', /转账|付款|支付凭证|银行回单/],
          ['票据材料', /发票|收据|票据/],
          ['结算材料', /结算单|结算材料|结算申请/],
          ['签章完整性', /签章|盖章|印章|签字/],
          ['合同核心字段', /合同PDF核心|合同核心|合同字段|合同条款|协议条款/],
          ['特殊审批', /特殊审批|新增类型审批|审批材料|审批意见/],
          ['费用类型', /费用性质|费用类型|归类|重新分类|类型核验/],
          ['主体身份', /主体|身份|收款人|签订方|客户|经销商|资格|抬头/],
          ['商品对应', /商品|产品|条形码|69码|SKU|知识库|购赠|赠品/i],
          ['现场照片', /现场|照片|返图|活动证明|活动照片|物料成品|上架/],
          ['资料完整性', /资料|材料|缺失|缺少|未提交|不完整|不可核验|无法核验|未识别/],
        ];
        return rules.find(([, pattern]) => pattern.test(text))?.[0] || '资料完整性';
      };
      const normalizedErrorReasonCategories = (values, fallback) => {
        const sourceValues = Array.isArray(values) ? values : [values];
        const categories = [...new Set(sourceValues.map((value) => String(value || '').trim()).filter(Boolean))];
        if (!categories.length) categories.push(errorReasonCategory(fallback));
        return categories.sort((left, right) => {
          const leftIndex = ERROR_REASON_ORDER.indexOf(left);
          const rightIndex = ERROR_REASON_ORDER.indexOf(right);
          return (leftIndex < 0 ? ERROR_REASON_ORDER.length : leftIndex)
            - (rightIndex < 0 ? ERROR_REASON_ORDER.length : rightIndex)
            || left.localeCompare(right, 'zh-CN');
        });
      };
      const headerFiles = (sheet, index) => {
        const values = lines(sheet.headers?.[index] || '');
        return (values.length > 1 ? values.slice(1) : values).join('、');
      };
      const issueRows = (sheet) => (sheet.rows || []).filter((row) => row.status === 'issue');
      let errorSequence = 0;
      const collectedErrorReasonCategories = new Set();
      const textBlock = (value) => lines(value).map((line) => `<div class="eo-line">${escapeHtml(line)}</div>`).join('');
      // Saved reason fields are the shared backend projection. Legacy static files
      // have no such fields: retain explicit error facts, never whole ledger columns.
      const legacyReasonFacts = (value) => [...new Set(lines(value).flatMap((line) =>
        line.replace(/^(?:错误原因|识别结果|本项错误|具体错误)：\s*/, '')
          .replace(/^已盘点本包全部材料[，,]\s*/, '')
          .split(/[。；]/)
          .map((part) => part.trim())
          .filter((part) => part
            && !/^(?:材料要求|审核结论|核销影响|处理方式|主要问题|最终结论|本项结果|知识库结论|地点核验|地点置信度|置信度|建议|要重新提交|需要补交|读取限制|范围[\/／]缺口)[：:]/.test(part)
            && !/(?:建议|请补|请确认|重新提交|补交|补拍|转人工|人工核验|应当|应该|不能自动核销|暂不能|不代表|不能据此认定|具体文件数量|清单见下方|需要处理|仅作模糊辅助|不参与判错|不计为错误|现有材料不能判断|因此无法确认)/.test(part)
            && /(?:缺失|缺少|未提交|未发现|未识别|未显示|未见|未找到|未登记|未能|未在|未完成|未确认|未核验|不清晰|不完整|不够清晰|不一致|不匹配|不符|不足|不唯一|无法|不能确定|没有|重复|复用|相差|差额|差异|偏差|重叠|受.{0,8}限制|主件|合同门店：|照片水印地点：|申请金额.{0,20}元|转账.{0,20}元)/.test(part)
          )
      ))];
      const rowErrorReasons = (row, fallback = '') => {
        if (Array.isArray(row?.error_reasons)) return row.error_reasons.filter((value) => typeof value === 'string' && value.trim());
        if (typeof row?.error_reason === 'string') return lines(row.error_reason);
        return legacyReasonFacts(fallback);
      };
      const rowProblem = (row, fallback = '') => textBlock(rowErrorReasons(row, fallback).join('\n'));
      const groupedRowProblem = (sheet, entries, scope) => {
        const reasons = entries.flatMap((entry) => {
          const row = (sheet.rows || []).find((candidate) => candidate.heading === entry[0]);
          const scoped = row?.error_reasons_by_scope?.[scope];
          if (Array.isArray(scoped)) return scoped.filter((value) => typeof value === 'string' && value.trim());
          const locator = entry[0].replace(/^合同销售附件第(\d+)行｜PDF第(\d+)页$/, '合同第$2页第$1行');
          return legacyReasonFacts(`${locator}，商品“${entry[1]}”：${entry[3]}`);
        });
        return textBlock([...new Set(reasons)].join('\n'));
      };
      const businessFileName = (value) => String(value || '').replaceAll('\\', '/').split('/').filter(Boolean).pop() || '';
      const isBusinessFile = (value) => /\.(?:pdf|xlsx?|xlsm|jpe?g|png|webp|gif|bmp|tiff?|heic|zip|rar)$/i.test(String(value || '').trim())
        && !/(?:^|[\\/])(?:crops?|ocr|rendered|model-input|visual-staging|derived)(?:[\\/]|$)/i.test(String(value || ''));
      const evidenceHtml = (evidence, fallbackFiles = []) => {
        const detail = evidence && typeof evidence === 'object' ? evidence : null;
        const sources = Array.isArray(detail?.sources) ? detail.sources : [];
        const nonBusinessKinds = new Set(['reference', 'generated', 'model_generated', 'model_input', 'model_output', 'synthetic', 'rendered', 'crop', 'ocr']);
        const excluded = new Set(sources.filter((source) => nonBusinessKinds.has(source?.kind))
          .flatMap((source) => [source.file, source.original_file]).filter(Boolean));
        const sourceByFile = new Map(sources.filter((source) => source?.file).map((source) => [source.file, source]));
        const declared = Array.isArray(detail?.source_files) ? detail.source_files
          : Array.isArray(detail?.sources) ? sources.map((source) => source.file)
            : fallbackFiles.filter(isBusinessFile);
        const originalFile = (value) => {
          if (excluded.has(value)) return '';
          const source = sourceByFile.get(value);
          if (source?.kind === 'derived') return source.original_file || '';
          return source?.original_file || value;
        };
        // Deduplicate identities before reducing paths to names. Distinct submitted
        // files with the same basename remain separate entries. Structured original
        // sources are authoritative even for unsupported extensions or an "ocr" folder.
        const originals = [...new Set(declared.map(originalFile)
          .filter((value) => typeof value === 'string' && value.trim() && !excluded.has(value)))];
        if (!originals.length) return '';
        return `<section class="eo-evidence" aria-label="涉及的业务文件"><h4 class="eo-evidence-count">涉及的业务文件</h4>${originals.map((file) => `<div class="eo-evidence-source"><strong>${escapeHtml(businessFileName(file))}</strong></div>`).join('')}</section>`;
      };
      const actionText = (row) => {
        const value = String((row.values || []).find((value) => /^处理方式：/.test(String(value))) || row.values?.[5] || '');
        const match = /(?:处理方式|要重新提交(?:什么)?)：/.exec(value);
        if (!match) return '';
        const action = value.slice(match.index + match[0].length)
          .split(/(?:处理方式|要重新提交(?:什么)?|错误原因|主要问题|材料要求|审核结论|核验结果|核销影响|置信度)[：:]/, 1)[0];
        return lines(action).map((line) => line.replace(/^\d+\.\s*/, '').replace(/[；;\s]+$/, ''))
          .filter(Boolean).join('；');
      };
      const errorCard = ({ title, auditType, source = '', baseline = '', problem, action, extra = '', search = '', confidence = 'high', confidenceScore = null, reasonCategories = [], evidence = null }) => {
        const categories = normalizedErrorReasonCategories(reasonCategories, title);
        const confidenceView = confidencePresentation(
          confidence,
          confidenceScore,
          [title, source, baseline, problem, action, search].join(' '),
        );
        categories.forEach((category) => collectedErrorReasonCategories.add(category));
        const categoryChips = categories
          .map((category) => `<span class="eo-chip eo-error-reason">${escapeHtml(category)}</span>`)
          .join('');
        const problemHtml = String(problem || '');
        const categorizedProblem = problemHtml.startsWith('<div class="eo-chips">')
          ? problemHtml.replace('<div class="eo-chips">', `<div class="eo-chips">${categoryChips}`)
          : `<div class="eo-chips eo-error-reason-list">${categoryChips}</div>${problemHtml}`;
        const sequence = ++errorSequence;
        const accessibleName = `错误项 ${sequence}；核销类型：${auditType}；${title}；错误原因分类：${categories.join('、')}`;
        return `
          <article class="eo-error-card" aria-label="${escapeHtml(accessibleName)}" data-audit-type="${escapeHtml(auditType)}" data-error-categories="${escapeHtml(categories.join('|'))}" data-error-confidence="${confidenceView.level}" data-error-confidence-score="${confidenceView.scoreText}" data-search="${escapeHtml([title, auditType, categories.join(' '), source, baseline, problem, action, search, JSON.stringify(evidence || {})].join(' ').toLocaleLowerCase('zh-CN'))}">
            <div class="eo-error-index">${String(sequence).padStart(2, '0')}</div>
            <div class="eo-error-main">
              <header class="eo-error-head"><h3>${escapeHtml(title)}</h3><div class="eo-error-badges"><span class="eo-scope">核销类型 · ${escapeHtml(auditType)}</span></div></header>
              <div class="eo-field"><span>错误原因</span><div>${categorizedProblem}</div></div>
              ${action ? `<div class="eo-field action"><span>处理方式</span><div>${escapeHtml(action)}</div></div>` : ''}
              ${evidenceHtml(evidence, [source, baseline].filter(Boolean).flatMap((value) => value.split('、')).filter((value) => /\.(pdf|xlsx?|jpe?g|png|webp)$/i.test(value)))}
              ${extra}
            </div>
          </article>`;
      };
      const detailsTable = (label, headers, rows) => {
        if (!rows.length) return '';
        const head = headers.map((header) => `<th>${escapeHtml(header)}</th>`).join('');
        const body = rows.map((row) => `<tr data-search="${escapeHtml(row.join(' ').toLocaleLowerCase('zh-CN'))}">${row.map((cell, index) => `<td data-label="${escapeHtml(headers[index])}">${textBlock(cell)}</td>`).join('')}</tr>`).join('');
        return `<details class="eo-error-details"><summary>${escapeHtml(label)}</summary><div class="eo-table-wrap"><table class="eo-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></details>`;
      };

      const personnelView = (sheet) => {
        const rows = issueRows(sheet);
        const settlementCount = rows.filter((row) => row.section === 'settlement').length;
        const excelFile = headerFiles(sheet, 1);
        const settlementFile = headerFiles(sheet, 2);
        const transferFiles = headerFiles(sheet, 3);
        const cards = rows.map((row) => {
          let source = settlementFile;
          let baseline = excelFile;
          let reason = row.values?.[4] || row.values?.[3] || row.values?.[5];
          if (row.heading === '实际申请金额') {
            source = `${settlementFile}、${transferFiles}`;
            baseline = '';
            const claimed = firstNumber(afterPrefix(row.values?.[2], '视觉识别申请：'));
            const transferred = firstNumber(afterPrefix(row.values?.[3], '视觉识别转账：'));
            const difference = claimed === null || transferred === null ? null : Math.abs(claimed - transferred);
            reason = `结算单申请金额${displayNumber(claimed)}元，转账合计${displayNumber(transferred)}元，相差${displayNumber(difference)}元。`;
          } else if (row.heading === '收款人与日期') {
            source = transferFiles;
            baseline = excelFile;
            reason = '转账凭证未完整显示每笔收款人、对应门店和完整交易日期。';
          }
          const problem = rowProblem(row, reason);
          const reasonCategories = row.heading === '实际申请金额'
            ? ['金额复算']
            : row.heading === '收款人与日期'
              ? ['收款人与日期']
              : row.section === 'detail'
                ? ['商品对应']
                : [errorReasonCategory([row.heading, ...(row.values || [])].join(' '))];
          return errorCard({ title: row.heading, auditType: '人员激励', source, baseline, problem, action: actionText(row), search: (row.values || []).join(' '), confidence: row.confidence, confidenceScore: row.confidence_score, reasonCategories, evidence: row.card_evidence });
        }).join('');
        return { count: rows.length, upstream: settlementCount, local: rows.length - settlementCount, html: cards };
      };

      const displayGlobalLabels = new Set([
        '合同核心六项', '合同核心七项', '销售Excel对合同', '销售Excel对合同附件',
        '销售Excel文件内部', '合同商品知识库', '现场商品与合同',
      ]);
      const displayIssueLabels = (row) => {
        const line = lineWith(row.values?.[5], '主要问题：');
        return line ? line.slice('主要问题：'.length).split('、').map((item) => item.trim()).filter(Boolean) : [];
      };
      const photoKnowledgeConfirmed = (row) => {
        const saved = [lineWith(row.values?.[1], '知识库结论：'), lineWith(row.values?.[1], '现场商品：')].join('\n');
        return /置信度[：:]\s*(?:高|中)/.test(saved)
          && !/置信度[：:]\s*低|未确认|未识别|未找到|不匹配|无法/.test(saved);
      };
      const displayLocalLabels = (row) => displayIssueLabels(row).filter((label) => !displayGlobalLabels.has(label)
        && !(label === '现场商品知识库' && photoKnowledgeConfirmed(row)));
      const displayLocalLabel = (row, label) => {
        if (!['合同门店', '门店水印错误', '门店地点待核验', '门店地点低置信度', '门店水印缺失或无法核对'].includes(label)) return label;
        if (['门店水印错误', '门店地点待核验', '门店地点低置信度'].includes(label)) return '门店地点低置信度';
        if (label === '门店水印缺失或无法核对') return '门店无法确认';
        if (label !== '合同门店') return label;
        return String(row.values?.[3] || '').includes('门店不一致') ? '门店地点低置信度' : '门店无法确认';
      };
      const displayLocalReasonCategories = (row, labels) => normalizedErrorReasonCategories(
        labels.map((label) => displayLocalLabel(row, label)),
        [row.heading, ...(row.values || [])].join(' '),
      );
      const incompleteDisplayReviewReason = '现场照片复核未完成，尚未确认陈列列数和堆头面积';
      const blockedDisplayReadReason = '系统无法读取现场原图，陈列列数和堆头面积尚未核验';
      const displayReviewIncomplete = (row) => {
        if (rowErrorReasons(row).some((reason) => reason.includes(incompleteDisplayReviewReason) || reason.includes(blockedDisplayReadReason))) return true;
        const basis = afterPrefix(row.values?.[1], '视觉依据：');
        return /复核/.test(basis)
          && /必做|规定|要求|独立|原始分辨率|访问限制|技能|规则|schema/.test(basis)
          && /未(?:能)?完成|无法完成|未能.{0,30}重新打开|受.{0,12}限制阻断|无法读取/.test(basis);
      };
      const displayReadBlocked = (row) => rowErrorReasons(row).some((reason) => reason.includes(blockedDisplayReadReason))
        || (displayReviewIncomplete(row) && /拒绝访问|无法读取|(?:访问|文件).{0,12}(?:限制|阻断)|PermissionError|EACCES/i.test(afterPrefix(row.values?.[1], '视觉依据：')));
      const localDisplayProblem = (row, labels) => {
        const facts = [];
        const photo = row.values?.[1] || '';
        const compare = row.values?.[3] || '';
        if (labels.includes('活动日期')) facts.push(lineWith(photo, '识别日期：'));
        const hasStoreLowConfidence = labels.includes('门店地点低置信度')
          || labels.includes('门店水印错误')
          || (labels.includes('合同门店') && compare.includes('门店不一致'));
        const hasStoreEvidenceGap = labels.includes('门店水印缺失或无法核对')
          || (labels.includes('合同门店') && !compare.includes('门店不一致'));
        if (hasStoreLowConfidence || hasStoreEvidenceGap) {
          const contractStore = lineWith(row.values?.[0], '门店：').replace(/^门店：/, '') || row.heading;
          const visibleLocation = lineWith(photo, '识别地点：').replace(/^识别地点：/, '') || '未识别';
          const locationBasis = lineWith(photo, '地点核验依据：').replace(/^地点核验依据：/, '');
          if (hasStoreLowConfidence) {
            facts.push(`合同门店：${contractStore}`);
            facts.push(`照片水印地点：${visibleLocation}`);
            facts.push(locationBasis || '合同门店与照片水印地点不一致，同址或临近关系无法确认。');
          } else {
            facts.push(`合同门店：${contractStore}`);
            facts.push(`照片水印地点：${visibleLocation}`);
            facts.push('现场照片未显示可与合同门店对应的名称或地址。');
          }
        }
        if (labels.includes('陈列标准')) {
          if (displayReviewIncomplete(row)) facts.push(displayReadBlocked(row) ? blockedDisplayReadReason : incompleteDisplayReviewReason);
          else {
            facts.push(lineWith(photo, '陈列标准核验：'));
            facts.push(lineWith(photo, '视觉依据：'));
          }
        }
        if (labels.includes('现场商品知识库')) {
          facts.push('现场商品尚未确认');
        }
        const file = lineWith(photo, '文件：');
        if (file) facts.push(file);
        return facts.filter(Boolean);
      };
      const localDisplayAction = (row, labels) => {
        const actions = [];
        if (labels.includes('现场商品知识库')) actions.push('提供能辨认商品文字和完整包装的现场照片');
        if (labels.includes('陈列标准')) actions.push(displayReviewIncomplete(row)
          ? displayReadBlocked(row) ? '恢复现场原图读取后重新核验' : '重新复核现场原图'
          : '补一张完整堆头全景，能看清1平方米或数清4列');
        if (labels.includes('活动日期')) actions.push('补交能看清完整拍摄日期的现场照片');
        const hasStoreLowConfidence = labels.includes('门店地点低置信度')
          || labels.includes('门店水印错误')
          || (labels.includes('合同门店') && String(row.values?.[3] || '').includes('门店不一致'));
        const hasStoreEvidenceGap = labels.includes('门店水印缺失或无法核对')
          || (labels.includes('合同门店') && !String(row.values?.[3] || '').includes('门店不一致'));
        if (hasStoreLowConfidence || hasStoreEvidenceGap) {
          const contractStore = lineWith(row.values?.[0], '门店：').replace(/^门店：/, '') || row.heading;
          const visibleLocation = lineWith(row.values?.[1], '识别地点：').replace(/^识别地点：/, '') || '未识别';
          actions.push(hasStoreLowConfidence
            ? '补充能唯一证明两处同址、商场与店铺关系或实际距离的权威地址材料；若地图确认相距较远且无法证明关联，再提交水印地点正确的现场照片'
            : `补交能显示门店名称或地址、可与合同门店“${contractStore}”对应的现场照片`);
        }
        return actions.join('；');
      };
      const attachmentRows = (sheet) => (sheet.rows || []).filter((row) => /^合同销售附件第\d+行｜PDF第\d+页$/.test(row.heading));
      const knowledgeErrorRows = (sheet) => attachmentRows(sheet).map((row) => {
        const comparison = String(row.values?.[3] || '');
        const segment = comparison.split('\n\n合同附件 → 销售Excel\n')[0];
        const knowledgeProduct = lineWith(segment, '知识库商品：').replace(/^知识库商品：/, '') || '未确认';
        const unresolved = knowledgeProduct === '未确认'
          || knowledgeProduct === '未找到'
          || segment.includes('本项结果：商品存在条件未满足');
        if (!unresolved) return null;
        const contractCode = fieldValue(row.values?.[0], '商品编码') || '未识别';
        const reason = segment.includes('条形码：精确匹配')
          ? `合同商品编码“${contractCode}”未在商品资料中登记`
          : '合同商品的69码未能对应商品资料';
        const knowledgeResult = knowledgeProduct;
        return [row.heading, fieldValue(row.values?.[0], '商品名称') || '商品名称未识别', knowledgeResult, reason];
      }).filter(Boolean);
      const salesErrorRows = (sheet) => {
        const accepted = new Map([
          ['客户名称', new Set(['精确匹配'])], ['业务日期', new Set(['一致'])],
          ['商品编码', new Set(['精确匹配'])],
          ['条形码', new Set(['精确匹配'])], ['数量', new Set(['一致'])],
          ['零售价', new Set(['一致'])], ['合计金额', new Set(['一致'])],
          ['合同附件行内金额', new Set(['一致'])],
        ]);
        const rows = [];
        const unpairedContractRows = [];
        attachmentRows(sheet).forEach((row) => {
          const comparison = String(row.values?.[3] || '');
          const parts = comparison.split('\n\n合同附件 → 销售Excel\n');
          if (parts.length < 2) return;
          if (String(row.values?.[2] || '').includes('销售Excel：未找到唯一对应行')) {
            unpairedContractRows.push(row);
            return;
          }
          const errors = [];
          lines(parts[1]).forEach((line) => {
            const colon = line.indexOf('：');
            if (colon < 1) return;
            const field = line.slice(0, colon);
            const result = line.slice(colon + 1);
            if (accepted.has(field) && !accepted.get(field).has(result)) errors.push(`${field}：${result}`);
          });
          if (errors.length) rows.push([row.heading, fieldValue(row.values?.[0], '商品名称') || '商品名称未识别', lineWith(row.values?.[2], '销售Excel第') || lines(row.values?.[2])[0] || '未找到', errors.join('\n')]);
        });
        const unpairedExcelRows = (sheet.rows || []).filter((row) => row.status === 'issue' && row.heading.startsWith('合同销售附件未找到Excel'));
        const usedExcelRows = new Set();
        const clean = (value) => String(value || '').replace(/\s+/g, '').replaceAll('－', '-').replaceAll('—', '-');
        const hasValue = (value) => value && value !== '未识别';
        const locatorFields = ['客户名称', '业务日期', '条形码', '数量', '零售价', '合计金额'];
        unpairedContractRows.forEach((contractRow) => {
          const candidates = unpairedExcelRows.filter((excelRow) => {
            if (usedExcelRows.has(excelRow.heading)) return false;
            return locatorFields.every((field) => {
              const contractValue = fieldValue(contractRow.values?.[0], field);
              const excelValue = fieldValue(excelRow.values?.[2], field);
              return hasValue(contractValue) && hasValue(excelValue) && clean(contractValue) === clean(excelValue);
            });
          });
          if (candidates.length !== 1) {
            rows.push([contractRow.heading, fieldValue(contractRow.values?.[0], '商品名称') || '商品名称未识别', '销售Excel未找到唯一对应行', '未能按69码和交易字段唯一定位销售Excel行']);
            return;
          }
          const excelRow = candidates[0];
          usedExcelRows.add(excelRow.heading);
          const contractCode = fieldValue(contractRow.values?.[0], '商品编码');
          const excelCode = fieldValue(excelRow.values?.[2], '商品编码');
          const codeProblem = !hasValue(contractCode)
            ? `商品编码：合同附件未识别（销售Excel为 ${excelCode || '未识别'}）`
            : clean(contractCode) !== clean(excelCode)
              ? `商品编码：不一致（合同附件 ${contractCode}；销售Excel ${excelCode || '未识别'}）`
              : '';
          if (codeProblem) {
            rows.push([
              contractRow.heading,
              fieldValue(contractRow.values?.[0], '商品名称') || '商品名称未识别',
              lines(excelRow.values?.[2])[0] || '销售Excel行',
              codeProblem,
            ]);
          }
        });
        unpairedExcelRows.filter((row) => !usedExcelRows.has(row.heading)).forEach((row) => {
          rows.push([row.heading, '合同附件未找到对应基准', lines(row.values?.[2])[0] || '销售Excel行', '未能按69码和交易字段唯一定位合同附件行']);
        });
        return rows;
      };
      const displayView = (sheet) => {
        const activity = (sheet.rows || []).find((row) => row.heading === '活动概况｜合同PDF主核销文件');
        const attachment = (sheet.rows || []).find((row) => row.heading === '合同销售附件｜合同主基准');
        const contractFile = headerFiles(sheet, 0);
        const salesFile = headerFiles(sheet, 2);
        const knowledgeRows = knowledgeErrorRows(sheet);
        const salesRows = salesErrorRows(sheet);
        const upstream = [];
        if (activity?.status === 'issue') {
          const contractProblem = [lineWith(activity.values?.[3], '合同PDF核心七项：'), lines(activity.values?.[0]).find((line) => line.includes('水印：') && line.includes('不通过'))].filter(Boolean);
          const blockingProblem = contractProblem.filter((line) => !line.includes('水印'));
          const blockingAction = actionText(activity).split('；').filter((item) => item && !item.includes('水印')).join('；');
          if (blockingProblem.length || blockingAction) {
            upstream.push({ title: '合同PDF核心字段未通过', scope: '合同材料', source: contractFile, problem: rowProblem(activity, blockingProblem.join('\n')), action: blockingAction, search: (activity.values || []).join(' '), confidence: activity.confidence, confidenceScore: activity.confidence_score, reasonCategories: ['合同核心字段'] });
          }
        }
        if (knowledgeRows.length) {
          upstream.push({
            title: `有 ${knowledgeRows.length} 行合同商品资料无法对应`, scope: '合同商品',
            source: contractFile, baseline: '参半商品知识库',
            problem: groupedRowProblem(sheet, knowledgeRows, 'knowledge'),
            action: '在商品资料中登记合同使用的商品编码，或更正合同中填写有误的商品编码。',
            search: knowledgeRows.flat().join(' '),
            confidence: 'high',
            reasonCategories: ['商品对应'],
          });
        }
        if (salesRows.length) {
          upstream.push({
            title: `合同与销售Excel有 ${salesRows.length} 行未对齐`, scope: '销售明细',
            source: salesFile, baseline: contractFile,
            problem: groupedRowProblem(sheet, salesRows, 'sales'),
            action: '更正已列出的缺失或不一致字段，并使销售Excel与合同附件逐行对应。',
            search: salesRows.flat().join(' '),
            confidence: 'high',
            reasonCategories: ['销售明细'],
          });
        }
        const upstreamCards = upstream.map((item) => errorCard({ ...item, auditType: '堆头/陈列', evidence: sheet.card_evidence?.[item.baseline === '参半商品知识库' ? 'knowledge' : item.source === salesFile ? 'sales' : 'core'] })).join('');
        const localRows = (sheet.rows || []).filter((row) => row.section === 'detail' && row.status === 'issue' && displayLocalLabels(row).length);
        const localCards = localRows.map((row) => {
          const labels = displayLocalLabels(row);
          const facts = localDisplayProblem(row, labels);
          const problem = rowProblem(row, facts.join('\n'));
          const source = afterPrefix(row.values?.[1], '文件：') || headerFiles(sheet, 1);
          return errorCard({ title: row.heading, auditType: '堆头/陈列', source, baseline: contractFile, problem, action: localDisplayAction(row, labels), search: (row.values || []).join(' '), confidence: row.confidence, confidenceScore: row.confidence_score, reasonCategories: displayLocalReasonCategories(row, labels), evidence: row.card_evidence });
        }).join('');
        const html = `${upstreamCards}${localCards}`;
        return { count: upstream.length + localRows.length, upstream: upstream.length, local: localRows.length, html };
      };

      const posterView = (sheet) => {
        const rows = issueRows(sheet);
        const cards = rows.map((row) => {
          const problem = rowProblem(row, row.values?.[4] || row.values?.[1]);
          const source = afterPrefix(row.values?.[0], '来源：');
          const title = row.heading.replace(/^错误项：/, '');
          return errorCard({ title, auditType: '海报/展示道具', source, problem, action: actionText(row), search: (row.values || []).join(' '), confidence: row.confidence, confidenceScore: row.confidence_score, reasonCategories: [errorReasonCategory([title, ...(row.values || [])].join(' '))], evidence: row.card_evidence });
        }).join('');
        return { count: rows.length, upstream: rows.length, local: 0, html: cards };
      };

      const groupedIssueView = (sheet, auditType) => {
        const rows = issueRows(sheet);
        const cards = rows.map((row, index) => {
          const title = String(row.heading || '').replace(/^问题：/, '');
          const source = afterPrefix(row.values?.[0], '文件：');
          const problem = rowProblem(row, row.values?.[1]);
          const action = actionText(row);
          const issue = sheet.projection_kind === 'material_diagnostic' ? sheet.diagnostic_issues?.[index] : null;
          const reasonCategory = sheet.projection_kind === 'classification_rejection' || issue?.code === 'scenario_unconfirmed'
            ? '费用类型' : errorReasonCategory([title, ...(row.values || [])].join(' '));
          return errorCard({ title, auditType, source, problem, action, search: (row.values || []).join(' '), confidence: row.confidence, confidenceScore: row.confidence_score, reasonCategories: [reasonCategory], evidence: row.card_evidence });
        }).join('');
        return { count: rows.length, upstream: rows.length, local: 0, html: cards };
      };

      const projections = data.sheets.map((sheet) => {
        if (sheet.projection_kind === 'material_diagnostic' || sheet.projection_kind === 'classification_rejection') {
          const label = sheet.audit_type_label || (sheet.projection_kind === 'classification_rejection' ? '核销方式无法确认' : '核销类型待确认');
          return { sheet, ...groupedIssueView(sheet, label), label };
        }
        if (sheet.scenario === 'personnel_incentive') return { sheet, ...personnelView(sheet), label: '人员激励' };
        if (sheet.scenario === 'promotional_display') return { sheet, ...displayView(sheet), label: '堆头/陈列' };
        if (sheet.scenario === 'poster_material') return { sheet, ...posterView(sheet), label: '海报/展示道具' };
        if (sheet.scenario === 'other_expense') return { sheet, ...groupedIssueView(sheet, '其他费用'), label: '其他费用' };
        if (sheet.scenario === 'maintenance_fee') return { sheet, ...groupedIssueView(sheet, '维护费用'), label: '维护费用' };
        if (sheet.scenario === 'giveaway_promotion') return { sheet, ...groupedIssueView(sheet, '额外搭赠'), label: '额外搭赠' };
        if (sheet.scenario === 'price_difference_support') return { sheet, ...groupedIssueView(sheet, '价格补差'), label: '价格补差' };
        if (sheet.scenario === 'pos_target_incentive') return { sheet, ...groupedIssueView(sheet, 'POS达标激励'), label: 'POS达标激励' };
        if (sheet.scenario === 'entry_fee') return { sheet, ...groupedIssueView(sheet, '进场费'), label: '进场费' };
        if (sheet.scenario === 'self_procured_gift_material') return { sheet, ...groupedIssueView(sheet, '客户自采赠品物料'), label: '客户自采赠品物料' };
        return { sheet, ...groupedIssueView(sheet, '其他费用'), label: '其他费用' };
      });
      const total = projections.reduce((sum, item) => sum + item.count, 0);
      const typeCount = new Set(projections.filter((item) => item.count > 0 && item.sheet.scenario).map((item) => item.sheet.scenario)).size;
      const unifiedErrors = projections.map((item) => item.html).filter(Boolean).join('');
      const errorTypes = [...new Set(projections.map((item) => item.label))];
      const errorReasonValues = [
        ...ERROR_REASON_ORDER.filter((category) => collectedErrorReasonCategories.has(category)),
        ...[...collectedErrorReasonCategories].filter((category) => !ERROR_REASON_ORDER.includes(category)).sort((left, right) => left.localeCompare(right, 'zh-CN')),
      ];
      const errorTypeOptions = errorTypes.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(type)}</option>`).join('');
      const errorTypeAllOption = errorTypes.length === 1 ? '' : '<option value="">全部核销方式</option>';
      const errorReasonOptions = errorReasonValues.map((category) => `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`).join('');

      const fallbackPassGroups = data.sheets.map((sheet) => {
        const projection = projections.find((item) => item.sheet === sheet);
        const items = (sheet.rows || []).filter((row) => row.status === 'pass').map((row, index) => ({
          check_id: `legacy-${sheet.scenario}-${index + 1}`,
          category: '核销明细',
          title: '明细检查项通过',
          subject: row.heading || `第${row.excel_row || index + 1}行`,
          basis: [...(row.values || [])].reverse().find(Boolean) || '检查结果已通过。',
          source_files: [],
          source_file_count: 0,
          confidence: row.confidence || 'high',
          confidence_score: row.confidence_score,
          scope: row.section === 'detail' ? 'product' : 'material',
        }));
        return {
          scenario: sheet.scenario,
          audit_type: projection?.label || sheet.name || '其他核销',
          title: sheet.name || projection?.label || '其他核销',
          item_count: items.length,
          category_counts: items.length ? { 核销明细: items.length } : {},
          items,
        };
      });
      const suppliedPassLog = data.pass_check_log && typeof data.pass_check_log === 'object' ? data.pass_check_log : null;
      const passGroups = Array.isArray(suppliedPassLog?.groups) ? suppliedPassLog.groups : fallbackPassGroups;
      const passItems = passGroups.flatMap((group) => Array.isArray(group.items) ? group.items : []);
      const passTotal = passItems.length;
      const passTypes = [...new Set(passGroups
        .map((group) => group.audit_type || group.title || '其他核销'))];
      const passCategories = [...new Set(passItems.map((item) => item.category || '核销检查'))];
      const passTypeOptions = passTypes.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(type)}</option>`).join('');
      const passTypeAllOption = passTypes.length === 1 ? '' : '<option value="">全部核销方式</option>';
      const passCategoryOptions = passCategories.map((category) => `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`).join('');
      const sourceFiles = (item) => {
        return evidenceHtml(item.evidence, item.source_files || []);
      };
      const buildPassGroupHtml = () => {
        let passSequence = 0;
        return passGroups.map((group, groupIndex) => {
        const items = Array.isArray(group.items) ? group.items : [];
        const auditType = group.audit_type || group.title || '其他核销';
        const categories = new Map();
        items.forEach((item) => {
          const category = item.category || '核销检查';
          if (!categories.has(category)) categories.set(category, []);
          categories.get(category).push(item);
        });
        const categoryHtml = [...categories.entries()].map(([category, categoryItems]) => {
          const cards = categoryItems.map((item) => {
            passSequence += 1;
            const itemTitle = item.title || '检查项通过';
            const itemSubject = item.subject || auditType || '本次材料';
            const confidenceView = confidencePresentation(
              item.confidence,
              item.confidence_score,
              [category, item.title, item.subject, item.basis, ...(item.source_files || [])].join(' '),
            );
            const accessibleLabel = `正确检查项 ${passSequence}；核销类型：${auditType}；检查分类：${category}；${itemTitle}；核验对象：${itemSubject}`;
            return `<article class="eo-pass-card" data-pass-check="${escapeHtml(item.check_id || `pass-${passSequence}`)}" data-pass-category="${escapeHtml(category)}" data-pass-confidence="${confidenceView.level}" data-pass-confidence-score="${confidenceView.scoreText}" aria-label="${escapeHtml(accessibleLabel)}">
              <div class="eo-pass-index"><span>PASS</span><strong>${String(passSequence).padStart(4, '0')}</strong></div>
              <div class="eo-pass-main"><header class="eo-pass-head"><div><small>${escapeHtml(category)}</small><h3>${escapeHtml(itemTitle)}</h3></div></header>
                <div class="eo-pass-field"><span>核验对象</span><strong>${escapeHtml(itemSubject)}</strong></div>
                <div class="eo-pass-field"><span>判断依据</span><div>${textBlock(item.basis || '结构化核销结果已确认该检查项通过。')}</div></div>
                ${sourceFiles(item)}
              </div>
            </article>`;
          }).join('');
          return `<section class="eo-pass-category" data-pass-category-block="${escapeHtml(category)}"><header><span>${escapeHtml(category)}</span><em data-pass-category-visible>${categoryItems.length} 项</em></header><div class="eo-pass-stack">${cards}</div></section>`;
        }).join('');
        const categorySummary = [...categories.entries()].map(([label, categoryItems]) => `<span data-pass-breakdown-category="${escapeHtml(label)}">${escapeHtml(label)} <b data-pass-breakdown-visible>${categoryItems.length}</b></span>`).join('');
        return `<section class="eo-pass-group" data-pass-scenario="${escapeHtml(group.scenario || `group-${groupIndex}`)}" data-pass-type="${escapeHtml(auditType)}">
          <header class="eo-pass-group-head"><div class="eo-pass-group-code">${String(groupIndex + 1).padStart(2, '0')}<small>TYPE</small></div><div><small>核销类型 · ${escapeHtml(auditType)}</small><h2>${escapeHtml(group.title || group.audit_type || '正确检查项')}</h2><div class="eo-pass-breakdown">${categorySummary || '<span>暂无通过项</span>'}</div></div><div class="eo-pass-group-count"><span>当前显示</span><strong data-pass-group-visible>${items.length}</strong><em>项</em></div></header>
          ${categoryHtml || '<div class="eo-pass-empty"><strong>本类型暂无可独立确认的通过项</strong><span>这不代表未执行核验；没有充分证据的检查不会被写成通过。</span></div>'}
        </section>`;
        }).join('');
      };

      document.body.className = 'error-only-page';
      document.body.innerHTML = `<header class="eo-topbar"><div class="eo-topbar-inner">
        <div class="eo-brand"><div class="eo-brand-mark">参半<small>CANBAN</small></div><div class="eo-brand-copy"><strong>离线活动核销</strong><span>核销判断工作台 / AUDIT DESK</span></div></div>
      </div></header><main class="eo-shell"><div class="eo-app-grid"><aside class="eo-rail" aria-label="核销结果导航">
        <div class="eo-rail-head"><div><div class="eo-rail-code">CB / OFFLINE AUDIT</div><h2>核销主工作台</h2></div><p>错误与正确检查日志</p></div>
        <nav class="eo-rail-nav" role="tablist" aria-label="核销结果视图" aria-orientation="vertical"><button class="eo-tab" id="eo-tab-home" role="tab" type="button" data-eo-view="home" aria-selected="true" aria-current="page" aria-controls="home" tabindex="0"><span class="eo-nav-glyph">OV</span><span class="eo-nav-copy"><strong>错误总览</strong><small>本批次全部错误</small></span><em>${total}</em></button><button class="eo-tab eo-tab-pass" id="eo-tab-passed" role="tab" type="button" data-eo-view="passed" aria-selected="false" aria-controls="passed" tabindex="-1"><span class="eo-nav-glyph">OK</span><span class="eo-nav-copy"><strong>正确检查项</strong><small>按核销类型记录</small></span><em>${passTotal}</em></button></nav>
        <div class="eo-rail-foot"><span>本次核销记录</span><strong>${Math.max(typeCount, passGroups.length)} 种核销类型</strong><em>${total} 组错误 · ${passTotal} 项通过</em></div>
      </aside><div class="eo-workspace"><section class="eo-view" id="home" role="tabpanel" aria-labelledby="eo-tab-home" tabindex="0"><div class="eo-home-cockpit">
        <section class="eo-pass-filter-panel eo-error-filter-panel" aria-label="筛选错误检查项">
          <div class="eo-pass-filter-grid eo-error-filter-grid"><label><span>核销方式</span><select id="eoErrorType" aria-controls="eoErrorList">${errorTypeAllOption}${errorTypeOptions}</select></label><label><span>错误原因分类</span><select id="eoErrorCategory" aria-controls="eoErrorList"><option value="">全部错误原因分类</option>${errorReasonOptions}</select></label></div>
        </section>
        <section class="eo-queue-panel"><div class="eo-scenario-grid eo-stack" id="eoErrorList">${unifiedErrors || '<div class="eo-empty">没有发现核销错误</div>'}</div></section>
        <div class="eo-pass-filter-empty eo-error-filter-empty" id="eoErrorFilterEmpty" hidden><strong>没有符合条件的错误检查项</strong><span>可调整核销方式或错误原因分类后重试。</span><button type="button" data-error-reset>清除全部筛选</button></div>
      </div></section><section class="eo-view" id="passed" role="tabpanel" aria-labelledby="eo-tab-passed" tabindex="0" hidden><div class="eo-home-cockpit eo-pass-cockpit">
        <section class="eo-pass-filter-panel" aria-label="筛选正确检查项">
          <div class="eo-pass-filter-grid"><label><span>核销方式</span><select id="eoPassType" aria-controls="eoPassList">${passTypeAllOption}${passTypeOptions}</select></label><label><span>检查分类</span><select id="eoPassCategory" aria-controls="eoPassList"><option value="">全部检查分类</option>${passCategoryOptions}</select></label></div>
          <div class="eo-pass-facet-hint eo-facet-summary-only"><em id="eoPassFacetSummary" aria-live="polite"></em></div>
        </section>
        <section class="eo-pass-ledger"><div class="eo-pass-groups" id="eoPassList"></div></section>
        <div class="eo-pass-filter-empty" id="eoPassFilterEmpty" hidden><strong>没有符合条件的正确检查项</strong><span>可调整核销方式或检查分类后重试。</span><button type="button" data-pass-reset>清除全部筛选</button></div>
      </div></section></div></div></main><button class="eo-back-top" id="eoBackTop" type="button">返回顶部</button>`;

      document.getElementById('eoBackTop').addEventListener('click', () => window.scrollTo({
        top: 0,
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
      }));
      const renderFacetOptions = ({ select, values, allLabel, label, counts, selected, omitAllWhenSingle = false }) => {
        const availableValues = values.filter((value) => counts.has(value));
        if (selected && !availableValues.includes(selected)) availableValues.unshift(selected);
        const singleWithoutAll = omitAllWhenSingle && availableValues.length === 1;
        const allOption = document.createElement('option');
        allOption.value = '';
        allOption.textContent = allLabel;
        const options = availableValues.map((value) => {
          const option = document.createElement('option');
          const count = counts.get(value) || 0;
          option.value = value;
          option.textContent = count
            ? `${label(value)}（${count}）`
            : selected === value
              ? `${label(value)}（当前选择 · 0）`
              : `${label(value)}（0）`;
          return option;
        });
        select.replaceChildren(...(singleWithoutAll ? options : [allOption, ...options]));
        select.value = singleWithoutAll ? availableValues[0] : selected;
        select.disabled = availableValues.length === 0;
        return counts.size;
      };

      const errorTypeFilter = document.getElementById('eoErrorType');
      const errorCategoryFilter = document.getElementById('eoErrorCategory');
      const errorFilterPanel = document.querySelector('.eo-error-filter-panel');
      const errorFilterEmpty = document.getElementById('eoErrorFilterEmpty');
      const errorFilterRecords = [...document.querySelectorAll('#eoErrorList .eo-error-card')].map((card) => ({
        card,
        type: card.dataset.auditType || '其他费用',
        categories: (card.dataset.errorCategories || '资料完整性').split('|').filter(Boolean),
      }));
      const orphanErrorTypes = [...new Set(errorFilterRecords
        .map((record) => record.type)
        .filter((type) => !errorTypes.includes(type)))];
      if (orphanErrorTypes.length) throw new Error(`错误原因缺少对应核销类型：${orphanErrorTypes.join('、')}`);
      const errorGlobalTypeCounts = errorFilterRecords.reduce((counts, record) => {
        counts.set(record.type, (counts.get(record.type) || 0) + 1);
        return counts;
      }, new Map(errorTypes.map((type) => [type, 0])));
      const errorFacetMeta = {
        type: {
          select: errorTypeFilter,
          values: errorTypes,
          allLabel: '全部核销方式',
          label: (value) => value,
        },
        category: {
          select: errorCategoryFilter,
          values: errorReasonValues,
          allLabel: '全部错误原因分类',
          label: (value) => value,
        },
      };
      const readErrorFilterState = () => ({
        type: errorTypeFilter.value,
        category: errorCategoryFilter.value,
      });
      const errorRecordMatches = (record, state, excludedFacet = '') => (
        (excludedFacet === 'type' || !state.type || record.type === state.type)
        && (excludedFacet === 'category' || !state.category || record.categories.includes(state.category))
      );
      const errorFacetCounts = (facet, state) => errorFilterRecords.reduce((counts, record) => {
        if (!errorRecordMatches(record, state, facet)) return counts;
        const values = facet === 'category' ? record.categories : [record[facet]];
        values.forEach((value) => counts.set(value, (counts.get(value) || 0) + 1));
        return counts;
      }, new Map());
      const renderErrorFacet = (facet, state) => {
        const meta = errorFacetMeta[facet];
        const counts = facet === 'type' ? errorGlobalTypeCounts : errorFacetCounts(facet, state);
        return renderFacetOptions({
          select: meta.select,
          values: meta.values,
          allLabel: meta.allLabel,
          label: meta.label,
          counts,
          selected: state[facet],
          omitAllWhenSingle: facet === 'type',
        });
      };
      renderErrorFacet('type', readErrorFilterState());
      const refreshErrorFacets = () => {
        const state = readErrorFilterState();
        renderErrorFacet('category', state);
        return readErrorFilterState();
      };
      const applyErrorFilters = () => {
        const state = refreshErrorFacets();
        let visibleTotal = 0;
        errorFilterRecords.forEach((record) => {
          const matches = errorRecordMatches(record, state);
          record.card.hidden = !matches;
          if (matches) visibleTotal += 1;
        });
        const filtering = Boolean((errorTypes.length > 1 && state.type) || state.category);
        errorFilterPanel.classList.toggle('is-filtering', filtering);
        errorFilterEmpty.hidden = visibleTotal !== 0 || total === 0;
      };
      const normalizeErrorDependentsForType = () => {
        const state = readErrorFilterState();
        const categoryCounts = errorFacetCounts('category', state);
        if (state.category && !categoryCounts.has(state.category)) errorCategoryFilter.value = '';
      };
      const resetErrorFilters = () => {
        errorTypeFilter.value = '';
        errorCategoryFilter.value = '';
        applyErrorFilters();
      };
      errorTypeFilter.addEventListener('change', () => {
        normalizeErrorDependentsForType();
        applyErrorFilters();
      });
      errorCategoryFilter.addEventListener('change', applyErrorFilters);
      document.querySelectorAll('[data-error-reset]').forEach((button) => button.addEventListener('click', resetErrorFilters));
      applyErrorFilters();

      let passLedgerInitialized = false;
      const initializePassLedger = () => {
      if (passLedgerInitialized) return;
      const passList = document.getElementById('eoPassList');
      passList.innerHTML = buildPassGroupHtml()
        || '<div class="eo-pass-empty"><strong>暂无正确检查项日志</strong><span>只有被结构化结果明确判定通过的子检查才会出现在这里。</span></div>';
      const passTypeFilter = document.getElementById('eoPassType');
      const passCategoryFilter = document.getElementById('eoPassCategory');
      const passFilterPanel = document.querySelector('#passed .eo-pass-filter-panel');
      const passFilterEmpty = document.getElementById('eoPassFilterEmpty');
      const passFacetSummary = document.getElementById('eoPassFacetSummary');
      const passFilterRecords = [...document.querySelectorAll('#eoPassList .eo-pass-card')].map((card) => ({
        card,
        type: card.closest('.eo-pass-group')?.dataset.passType || '其他核销',
        category: card.dataset.passCategory || '核销检查',
      }));
      const passFilterRecordByCard = new WeakMap(passFilterRecords.map((record) => [record.card, record]));
      const passFilterGroups = [...document.querySelectorAll('#eoPassList .eo-pass-group')].map((group) => ({
        group,
        visibleLabel: group.querySelector('[data-pass-group-visible]'),
        categories: [...group.querySelectorAll('.eo-pass-category')].map((block) => ({
          block,
          name: block.dataset.passCategoryBlock || '核销检查',
          visibleLabel: block.querySelector('[data-pass-category-visible]'),
          records: [...block.querySelectorAll('.eo-pass-card')]
            .map((card) => passFilterRecordByCard.get(card))
            .filter(Boolean),
        })),
        breakdowns: [...group.querySelectorAll('[data-pass-breakdown-category]')].map((badge) => ({
          badge,
          name: badge.dataset.passBreakdownCategory || '',
          visibleLabel: badge.querySelector('[data-pass-breakdown-visible]'),
        })),
      }));
      const passGlobalTypeCounts = passFilterRecords.reduce((counts, record) => {
        counts.set(record.type, (counts.get(record.type) || 0) + 1);
        return counts;
      }, new Map(passTypes.map((type) => [type, 0])));
      const passFacetMeta = {
        type: {
          select: passTypeFilter,
          values: passTypes,
          allLabel: '全部核销方式',
          label: (value) => value,
        },
        category: {
          select: passCategoryFilter,
          values: passCategories,
          allLabel: '全部检查分类',
          label: (value) => value,
        },
      };
      const readPassFilterState = () => ({
        type: passTypeFilter.value,
        category: passCategoryFilter.value,
      });
      const passRecordMatches = (record, state, excludedFacet = '') => (
        (excludedFacet === 'type' || !state.type || record.type === state.type)
        && (excludedFacet === 'category' || !state.category || record.category === state.category)
      );
      const passFacetCounts = (facet, state) => passFilterRecords.reduce((counts, record) => {
        if (!passRecordMatches(record, state, facet)) return counts;
        const value = record[facet];
        counts.set(value, (counts.get(value) || 0) + 1);
        return counts;
      }, new Map());
      const renderPassFacet = (facet, state) => {
        const meta = passFacetMeta[facet];
        const counts = facet === 'type' ? passGlobalTypeCounts : passFacetCounts(facet, state);
        return renderFacetOptions({
          select: meta.select,
          values: meta.values,
          allLabel: meta.allLabel,
          label: meta.label,
          counts,
          selected: state[facet],
          omitAllWhenSingle: facet === 'type',
        });
      };
      const passTypeFacetCount = renderPassFacet('type', readPassFilterState());
      const refreshPassFacets = () => {
        const state = readPassFilterState();
        const available = {
          type: passTypeFacetCount,
          category: renderPassFacet('category', state),
        };
        passFacetSummary.textContent = `${available.type} 种核销方式 · ${available.category} 类可选检查`;
        return readPassFilterState();
      };
      const applyPassFilters = () => {
        const state = refreshPassFacets();
        let visibleTotal = 0;
        passFilterGroups.forEach((groupRecord) => {
          let groupVisible = 0;
          const categoryCounts = new Map();
          groupRecord.categories.forEach((categoryRecord) => {
            let categoryVisible = 0;
            categoryRecord.records.forEach((record) => {
              const matches = passRecordMatches(record, state);
              record.card.hidden = !matches;
              if (matches) categoryVisible += 1;
            });
            categoryRecord.block.hidden = categoryVisible === 0;
            categoryCounts.set(categoryRecord.name, categoryVisible);
            if (categoryRecord.visibleLabel) categoryRecord.visibleLabel.textContent = `${categoryVisible} 项`;
            groupVisible += categoryVisible;
          });
          groupRecord.breakdowns.forEach((breakdown) => {
            const categoryCount = categoryCounts.get(breakdown.name) || 0;
            breakdown.badge.hidden = categoryCount === 0;
            if (breakdown.visibleLabel) breakdown.visibleLabel.textContent = String(categoryCount);
          });
          groupRecord.group.hidden = groupVisible === 0;
          if (groupRecord.visibleLabel) groupRecord.visibleLabel.textContent = String(groupVisible);
          visibleTotal += groupVisible;
        });
        const filtering = Boolean((passTypes.length > 1 && state.type) || state.category);
        passFilterPanel.classList.toggle('is-filtering', filtering);
        passFilterEmpty.hidden = visibleTotal !== 0 || passTotal === 0;
      };
      const resetPassFilters = () => {
        passTypeFilter.value = '';
        passCategoryFilter.value = '';
        applyPassFilters();
      };
      const normalizePassDependentsForType = () => {
        const state = readPassFilterState();
        const categoryCounts = passFacetCounts('category', state);
        if (state.category && !categoryCounts.has(state.category)) passCategoryFilter.value = '';
      };
      passTypeFilter.addEventListener('change', () => {
        normalizePassDependentsForType();
        applyPassFilters();
      });
      passCategoryFilter.addEventListener('change', applyPassFilters);
      document.querySelectorAll('[data-pass-reset]').forEach((button) => button.addEventListener('click', resetPassFilters));
      applyPassFilters();
      passLedgerInitialized = true;
      };
      const resultTabs = [...document.querySelectorAll('[data-eo-view]')];
      const openResultView = (viewId) => {
        if (viewId === 'passed') initializePassLedger();
        resultTabs.forEach((tab) => {
          const selected = tab.dataset.eoView === viewId;
          tab.setAttribute('aria-selected', String(selected));
          tab.setAttribute('tabindex', selected ? '0' : '-1');
          if (selected) tab.setAttribute('aria-current', 'page');
          else tab.removeAttribute('aria-current');
        });
        document.querySelectorAll('.eo-workspace > .eo-view').forEach((view) => {
          view.hidden = view.id !== viewId;
        });
        window.scrollTo({ top: 0, behavior: 'auto' });
      };
      resultTabs.forEach((tab) => tab.addEventListener('click', () => openResultView(tab.dataset.eoView)));
      document.querySelector('.eo-rail-nav').addEventListener('keydown', (event) => {
        if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        const current = Math.max(0, resultTabs.indexOf(event.target.closest('[data-eo-view]')));
        const next = event.key === 'Home' ? 0
          : event.key === 'End' ? resultTabs.length - 1
            : (current + (['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1) + resultTabs.length) % resultTabs.length;
        event.preventDefault();
        openResultView(resultTabs[next].dataset.eoView);
        resultTabs[next].focus();
      });
    })();
