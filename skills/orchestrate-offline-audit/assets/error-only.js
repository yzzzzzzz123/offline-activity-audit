    (() => {
      const source = document.getElementById('audit-data');
      if (!source) throw new Error('缺少核销数据');
      const data = JSON.parse(source.textContent);
      const style = document.getElementById('error-only-preview-style');
      document.head.appendChild(style);
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
      const headerFiles = (sheet, index) => {
        const values = lines(sheet.headers?.[index] || '');
        return (values.length > 1 ? values.slice(1) : values).join('、');
      };
      const issueRows = (sheet) => (sheet.rows || []).filter((row) => row.status === 'issue');
      const textBlock = (value) => lines(value).map((line) => `<div class="eo-line">${escapeHtml(line)}</div>`).join('');
      const actionText = (row) => {
        const value = String(row.values?.[5] || '');
        const match = value.match(/要重新提交(?:什么)?：([\s\S]*)/);
        if (!match) return '补充能够直接核验该错误的清晰材料后重新提交。';
        const items = lines(match[1]).map((line) => line.replace(/^\d+\.\s*/, '')).filter(Boolean);
        return items.join('；') || '补充能够直接核验该错误的清晰材料后重新提交。';
      };
      const errorCard = ({ index, title, scope, source = '', baseline = '', problem, action, extra = '', search = '' }) => `
        <article class="eo-error-card" data-search="${escapeHtml([title, scope, source, baseline, problem, action, search].join(' ').toLocaleLowerCase('zh-CN'))}">
          <div class="eo-error-index">${String(index).padStart(2, '0')}</div>
          <div class="eo-error-main">
            <header class="eo-error-head"><h3>${escapeHtml(title)}</h3><span class="eo-scope">${escapeHtml(scope)}</span></header>
            ${source ? `<div class="eo-field source"><span>问题文件</span><div>${escapeHtml(source)}</div></div>` : ''}
            ${baseline ? `<div class="eo-field source"><span>对照文件</span><div>${escapeHtml(baseline)}</div></div>` : ''}
            <div class="eo-field"><span>错误原因</span><div>${problem}</div></div>
            <div class="eo-field action"><span>处理方式</span><div>${escapeHtml(action)}</div></div>
            ${extra}
          </div>
        </article>`;
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
        const cards = rows.map((row, index) => {
          const scope = row.section === 'settlement' ? '结算与付款' : '商品核销';
          let source = settlementFile;
          let baseline = excelFile;
          let reason = row.values?.[4] || row.values?.[3] || row.values?.[5];
          if (row.heading === '实际申请金额') {
            source = `${settlementFile}、${transferFiles}`;
            baseline = '';
            const claimed = firstNumber(afterPrefix(row.values?.[2], '视觉识别申请：'));
            const transferred = firstNumber(afterPrefix(row.values?.[3], '视觉识别转账：'));
            const difference = claimed === null || transferred === null ? null : Math.abs(claimed - transferred);
            reason = `结算单识别申请金额为${displayNumber(claimed)}元，转账凭证识别金额为${displayNumber(transferred)}元，两份材料相差${displayNumber(difference)}元。现有材料不能判断应以哪一个金额为准，因此金额证据链未闭合。`;
          } else if (row.heading === '收款人与日期') {
            source = transferFiles;
            baseline = excelFile;
            const storeCount = firstNumber(lineWith(row.values?.[1], 'Excel读取到'));
            reason = `销售Excel读取到${displayNumber(storeCount)}家门店；转账凭证目前只能确认金额，没有完整显示每笔收款人、对应门店和完整交易日期。因此无法确认Excel中的${displayNumber(storeCount)}家门店分别由谁收款、对应哪一笔转账以及具体转账日期。`;
          }
          const problem = textBlock(reason);
          return errorCard({ index: index + 1, title: row.heading, scope, source, baseline, problem, action: actionText(row), search: (row.values || []).join(' ') });
        }).join('');
        return { count: rows.length, upstream: settlementCount, local: rows.length - settlementCount, html: cards || '<div class="eo-empty">没有发现核销错误</div>' };
      };

      const displayGlobalLabels = new Set([
        '合同核心六项', '合同核心七项', '销售Excel对合同', '销售Excel对合同附件',
        '销售Excel文件内部', '合同商品知识库', '现场商品与合同',
      ]);
      const displayIssueLabels = (row) => {
        const line = lineWith(row.values?.[5], '主要问题：');
        return line ? line.slice('主要问题：'.length).split('、').map((item) => item.trim()).filter(Boolean) : [];
      };
      const displayLocalLabels = (row) => displayIssueLabels(row).filter((label) => !displayGlobalLabels.has(label));
      const displayLocalLabel = (row, label) => {
        if (!['合同门店', '门店水印错误', '门店水印缺失或无法核对'].includes(label)) return label;
        if (label !== '合同门店') return label;
        return String(row.values?.[3] || '').includes('门店不一致') ? '门店水印错误' : '门店水印缺失或无法核对';
      };
      const localDisplayProblem = (row, labels) => {
        const facts = [];
        const photo = row.values?.[1] || '';
        const compare = row.values?.[3] || '';
        if (labels.includes('活动日期')) facts.push(lineWith(photo, '识别日期：'));
        const hasStoreMismatch = labels.includes('门店水印错误')
          || (labels.includes('合同门店') && compare.includes('门店不一致'));
        const hasStoreEvidenceGap = labels.includes('门店水印缺失或无法核对')
          || (labels.includes('合同门店') && !compare.includes('门店不一致'));
        if (hasStoreMismatch || hasStoreEvidenceGap) {
          const contractStore = lineWith(row.values?.[0], '门店：').replace(/^门店：/, '') || row.heading;
          const visibleLocation = lineWith(photo, '识别地点：').replace(/^识别地点：/, '') || '未识别';
          if (hasStoreMismatch) {
            facts.push(`合同门店：${contractStore}`);
            facts.push(`照片水印地点：${visibleLocation}`);
            facts.push(`水印地点已清楚识别，但“${visibleLocation}”与合同门店“${contractStore}”不一致；这是门店水印错误，不是照片没看清。文件名不能覆盖水印地点冲突。`);
          } else {
            facts.push(`合同门店：${contractStore}`);
            facts.push(`照片水印地点：${visibleLocation}`);
            facts.push('照片没有提供可与合同门店核对的水印地点；文件名不能单独证明门店。');
          }
        }
        if (labels.includes('陈列标准')) {
          facts.push(lineWith(photo, '陈列标准核验：'));
          facts.push(lineWith(photo, '视觉依据：'));
        }
        if (labels.includes('现场商品知识库')) {
          facts.push(lineWith(photo, '可见文字：'));
          facts.push(lineWith(photo, '现场商品：'));
          facts.push(lineWith(photo, '知识库结论：'));
        }
        const file = lineWith(photo, '文件：');
        if (file) facts.push(file);
        return facts.filter(Boolean);
      };
      const localDisplayAction = (row, labels) => {
        const actions = [];
        if (labels.includes('现场商品知识库')) actions.push('补拍同一商品包装：既要看清可对应知识库的名称片段、短码、规格或款式文字，也要保留足够完整的包装外观用于参考图视觉比对；文字与外观能唯一对应同一知识库商品即可，不强制拍到完整名称或69码');
        if (labels.includes('陈列标准')) actions.push('补一张完整堆头全景，能看清1平方米或数清4列');
        if (labels.includes('活动日期')) actions.push('补交能看清完整拍摄日期的现场照片');
        const hasStoreMismatch = labels.includes('门店水印错误')
          || (labels.includes('合同门店') && String(row.values?.[3] || '').includes('门店不一致'));
        const hasStoreEvidenceGap = labels.includes('门店水印缺失或无法核对')
          || (labels.includes('合同门店') && !String(row.values?.[3] || '').includes('门店不一致'));
        if (hasStoreMismatch || hasStoreEvidenceGap) {
          const contractStore = lineWith(row.values?.[0], '门店：').replace(/^门店：/, '') || row.heading;
          const visibleLocation = lineWith(row.values?.[1], '识别地点：').replace(/^识别地点：/, '') || '未识别';
          actions.push(hasStoreMismatch
            ? `重新提交水印地点正确的现场照片：当前水印为“${visibleLocation}”，应显示可与合同门店“${contractStore}”唯一对应的门店名称或地址；不是补拍清晰度`
            : `补交水印中能看清并可与合同门店“${contractStore}”唯一对应的门店名称或地址`);
        }
        return actions.join('；') || '补交能够直接证明本店现场执行情况的清晰照片。';
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
        const knowledgeCode = afterPrefix(segment, '知识库商品编码：') || '未确认';
        const reason = segment.includes('条形码：精确匹配')
          ? `产品编码：合同 ${contractCode}；知识库主编码 ${knowledgeCode}，且 product_code_aliases 未登记合同编码`
          : '69码未能精确确认知识库商品身份';
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
              `${codeProblem}\n69码、客户、日期、数量、单价和金额均一致；商品名称不参与判错`,
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
            upstream.push({ title: '合同PDF核心字段未通过', scope: '合同材料', source: contractFile, problem: textBlock(blockingProblem.join('\n')), action: blockingAction, search: (activity.values || []).join(' ') });
          }
        }
        if (knowledgeRows.length) {
          upstream.push({
            title: `合同商品有 ${knowledgeRows.length} 行产品编码未通过知识库`, scope: '合同商品',
            source: contractFile, baseline: '参半商品知识库',
            problem: `<div class="eo-chips"><span class="eo-chip">69码已命中，但合同产品编码未在知识库登记 ${knowledgeRows.length}行</span></div>`,
            action: '在知识库对应商品的 product_code_aliases 中登记合同业务产品编码，或更正合同中有误的产品编码；商品名称仍只作模糊辅助。',
            extra: detailsTable(`查看 ${knowledgeRows.length} 行合同商品错误清单`, ['合同位置', '合同商品', '知识库商品', '具体错误'], knowledgeRows),
            search: knowledgeRows.flat().join(' '),
          });
        }
        if (salesRows.length) {
          const pairedSalesRows = salesRows.filter((row) => String(row[0]).startsWith('合同销售附件第')).length;
          const summary = `合同附件 → 销售Excel：严格核对后有 ${salesRows.length} 行需要处理（其中 ${pairedSalesRows} 行已按69码及交易字段定位；商品名称仅作模糊辅助，不计为错误）。`;
          upstream.push({
            title: `合同与销售Excel有 ${salesRows.length} 行未对齐`, scope: '销售明细',
            source: salesFile, baseline: contractFile,
            problem: textBlock(summary),
            action: '只更正未识别或不一致的严格字段，并让销售Excel逐行对应合同附件；商品名称无需逐字一致。',
            extra: detailsTable(`查看 ${salesRows.length} 行销售错误清单`, ['合同位置', '合同商品', '销售Excel', '具体错误'], salesRows),
            search: salesRows.flat().join(' '),
          });
        }
        const upstreamCards = upstream.map((item, index) => errorCard({ index: index + 1, ...item })).join('');
        const localRows = (sheet.rows || []).filter((row) => row.section === 'detail' && row.status === 'issue' && displayLocalLabels(row).length);
        const localCards = localRows.map((row, index) => {
          const labels = displayLocalLabels(row);
          const facts = localDisplayProblem(row, labels);
          const problem = `<div class="eo-chips">${labels.map((label) => `<span class="eo-chip">${escapeHtml(displayLocalLabel(row, label))}</span>`).join('')}</div>${facts.length ? `<div style="margin-top:8px">${textBlock(facts.join('\n'))}</div>` : ''}`;
          const source = afterPrefix(row.values?.[1], '文件：') || headerFiles(sheet, 1);
          return errorCard({ index: index + 1, title: row.heading, scope: '门店现场', source, baseline: contractFile, problem, action: localDisplayAction(row, labels), search: (row.values || []).join(' ') });
        }).join('');
        const html = `${upstreamCards}<div class="eo-section-title"><h3>门店自身错误</h3><span>上游错误不在门店中重复</span></div>${localCards || '<div class="eo-empty">没有发现门店自身错误</div>'}`;
        return { count: upstream.length + localRows.length, upstream: upstream.length, local: localRows.length, html };
      };

      const posterView = (sheet) => {
        const rows = issueRows(sheet);
        const cards = rows.map((row, index) => {
          const problem = textBlock([row.values?.[1], row.values?.[4]].filter(Boolean).join('\n'));
          const source = afterPrefix(row.values?.[0], '来源：');
          return errorCard({ index: index + 1, title: row.heading.replace(/^错误项：/, ''), scope: '展示道具', source, problem, action: actionText(row), search: (row.values || []).join(' ') });
        }).join('');
        return { count: rows.length, upstream: rows.length, local: 0, html: cards || '<div class="eo-empty">没有发现核销错误</div>' };
      };

      const groupedIssueView = (sheet, scope) => {
        const rows = issueRows(sheet);
        const cards = rows.map((row, index) => {
          const title = String(row.heading || '').replace(/^问题：/, '');
          const source = afterPrefix(row.values?.[0], '文件：');
          const problem = textBlock([row.values?.[1], row.values?.[2], row.values?.[3], row.values?.[4]].filter(Boolean).join('\n'));
          const action = afterPrefix(row.values?.[5], '处理方式：') || actionText(row);
          return errorCard({ index: index + 1, title, scope, source, problem, action, search: (row.values || []).join(' ') });
        }).join('');
        return { count: rows.length, upstream: rows.length, local: 0, html: cards || '<div class="eo-empty">没有发现核销错误</div>' };
      };

      const projections = data.sheets.map((sheet) => {
        if (sheet.scenario === 'personnel_incentive') return { sheet, ...personnelView(sheet), label: '人员激励' };
        if (sheet.scenario === 'promotional_display') return { sheet, ...displayView(sheet), label: '堆头陈列' };
        if (sheet.scenario === 'poster_material') return { sheet, ...posterView(sheet), label: '展示道具' };
        if (sheet.scenario === 'other_expense') return { sheet, ...groupedIssueView(sheet, '其他费用'), label: '其他费用' };
        if (sheet.scenario === 'maintenance_fee') return { sheet, ...groupedIssueView(sheet, '维护费用'), label: '维护费用' };
        if (sheet.scenario === 'giveaway_promotion') return { sheet, ...groupedIssueView(sheet, '额外搭赠'), label: '额外搭赠' };
        if (sheet.scenario === 'price_difference_support') return { sheet, ...groupedIssueView(sheet, '价格补差'), label: '价格补差' };
        if (sheet.scenario === 'pos_target_incentive') return { sheet, ...groupedIssueView(sheet, 'POS达标激励'), label: 'POS达标激励' };
        if (sheet.scenario === 'entry_fee') return { sheet, ...groupedIssueView(sheet, '进场费'), label: '进场费' };
        if (sheet.scenario === 'self_procured_gift_material') return { sheet, ...groupedIssueView(sheet, '客户自采赠品物料'), label: '自采赠品物料' };
        return { sheet, ...groupedIssueView(sheet, '其他费用'), label: '其他费用' };
      });
      const total = projections.reduce((sum, item) => sum + item.count, 0);
      const materialTotal = projections.reduce((sum, item) => sum + (item.upstream || 0), 0);
      const fieldTotal = projections.reduce((sum, item) => sum + (item.local || 0), 0);
      const materialPercent = total ? Math.round(materialTotal / total * 100) : 0;
      const metaFor = (item, index) => {
        if (item.sheet.scenario === 'personnel_incentive') return {
          code: 'PI', queue: String(index + 1).padStart(2, '0'), accent: '#cf654f', eyebrow: 'PERSONNEL INCENTIVE', navNote: '商品 / 结算错误',
          note: '商品核销与结算付款中发现需要修正的项目。', parts: [['商品核销', item.local || 0], ['结算与付款', item.upstream || 0]],
        };
        if (item.sheet.scenario === 'promotional_display') return {
          code: 'PD', queue: String(index + 1).padStart(2, '0'), accent: '#d79a20', eyebrow: 'PROMOTIONAL DISPLAY', navNote: '合同 / 门店错误',
          note: '合同、销售明细与门店现场错误已分层归并。', parts: [['合同与销售', item.upstream || 0], ['门店现场', item.local || 0]],
        };
        if (item.sheet.scenario === 'poster_material') return {
          code: 'PR', queue: String(index + 1).padStart(2, '0'), accent: '#238b84', eyebrow: 'DISPLAY PROPS', navNote: '展示材料错误',
          note: '仅列展示道具材料中需要重新提交的错误。', parts: [['展示道具', item.upstream || item.count]],
        };
        if (item.sheet.scenario === 'maintenance_fee') return {
          code: 'MF', queue: String(index + 1).padStart(2, '0'), accent: '#2f75b5', eyebrow: 'MAINTENANCE FEE', navNote: '资料 / 金额错误',
          note: '仅列维护费用资料链、费用性质与金额复算中的处理项。', parts: [['资料与金额', item.upstream || item.count]],
        };
        if (item.sheet.scenario === 'giveaway_promotion') return {
          code: 'GP', queue: String(index + 1).padStart(2, '0'), accent: '#4f8b57', eyebrow: 'EXTRA GIVEAWAY', navNote: '搭赠 / 执行错误',
          note: '仅列额外搭赠合同、出货、结算、小票与活动执行中的处理项。', parts: [['搭赠与执行', item.upstream || item.count]],
        };
        if (item.sheet.scenario === 'price_difference_support') return {
          code: 'PD', queue: String(index + 1).padStart(2, '0'), accent: '#c55a11', eyebrow: 'PRICE DIFFERENCE', navNote: 'POS / 照片错误',
          note: '仅列价格补差合同、POS、结算、全门店活动价照片与金额复算中的处理项。', parts: [['补差与执行', item.upstream || item.count]],
        };
        if (item.sheet.scenario === 'pos_target_incentive') return {
          code: 'PT', queue: String(index + 1).padStart(2, '0'), accent: '#00a6a6', eyebrow: 'POS TARGET INCENTIVE', navNote: '达标 / 满减错误',
          note: '仅列经销商POS达标合同、销售基数、结算、满减活动证明和比例复算中的处理项。', parts: [['达标与活动', item.upstream || item.count]],
        };
        if (item.sheet.scenario === 'entry_fee') return {
          code: 'EF', queue: String(index + 1).padStart(2, '0'), accent: '#7f6000', eyebrow: 'ENTRY FEE', navNote: '合同 / 上架错误',
          note: '仅列进场费合同、合同门店和商品上架、系统扣款凭证与条码费复算中的处理项。', parts: [['进场与扣款', item.upstream || item.count]],
        };
        if (item.sheet.scenario === 'self_procured_gift_material') return {
          code: 'SG', queue: String(index + 1).padStart(2, '0'), accent: '#a64d79', eyebrow: 'SELF-PROCURED GIFT', navNote: '赠品 / POS错误',
          note: '仅列客户自采赠品物料合同、票据付款、盖章POS及电子表、全门店水印返图、结算和赠送数量复算中的处理项。', parts: [['赠品物料与执行', item.upstream || item.count]],
        };
        return {
          code: 'OE', queue: String(index + 1).padStart(2, '0'), accent: '#7b5aa6', eyebrow: 'OTHER EXPENSE', navNote: '归类 / 审批错误',
          note: '仅列其他费用归类、特殊审批与基础材料中的处理项。', parts: [['归类与审批', item.upstream || item.count]],
        };
      };
      const navItems = [{ view: 'home', selected: true, code: 'OV', label: '错误总览', note: '本批次全部错误', count: total }]
        .concat(projections.map((item, index) => {
          const meta = metaFor(item, index);
          return { view: `scenario-${index}`, selected: false, code: meta.code, label: item.label, note: meta.navNote, count: item.count };
        }));
      const nav = navItems.map((item) => `<button class="eo-tab" type="button" data-view="${item.view}" aria-selected="${item.selected}">
        <span class="eo-nav-glyph">${item.code}</span><span class="eo-nav-copy"><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.note)}</small></span><em>${item.count}</em>
      </button>`).join('');
      const scenarioCards = projections.map((item, index) => {
        const meta = metaFor(item, index);
        const parts = meta.parts.map(([label, value]) => `<span>${escapeHtml(label)} ${value}</span>`).join('');
        const searchText = `${item.label} ${meta.note} ${meta.parts.flat().join(' ')} ${item.html}`.replace(/<[^>]+>/g, ' ').toLocaleLowerCase('zh-CN');
        return `<article class="eo-scenario-card" style="--scenario-accent:${meta.accent}" data-search="${escapeHtml(searchText)}">
          <div class="eo-scenario-code">${meta.queue}<small>QUEUE</small></div>
          <div class="eo-scenario-copy"><small>${meta.eyebrow}</small><h2>${escapeHtml(item.label)}</h2><p>${escapeHtml(meta.note)}</p><div class="eo-breakdown">${parts}</div></div>
          <div class="eo-scenario-count"><span>待处理</span><strong>${item.count}</strong><em>项</em></div>
          <button class="eo-enter" type="button" data-open="scenario-${index}">进入错误清单&nbsp; →</button></article>`;
      }).join('');
      const views = projections.map((item, index) => `<section class="eo-view" id="scenario-${index}" hidden>
        <div class="eo-ledger"><div class="eo-page-command"><button type="button" data-open="home">← 返回错误总览</button><span>ERROR QUEUE / ${escapeHtml(metaFor(item, index).code)}</span></div><header class="eo-ledger-head"><div class="eo-ledger-title"><small>核销错误清单</small><h2>${escapeHtml(item.label)}</h2><p>本页只列需要处理的错误；通过项、合同基准和核销过程均已隐藏。</p></div><div class="eo-impact"><span>待处理</span><strong>${item.count} 项</strong></div></header><div class="eo-stack">${item.html}</div><div class="eo-no-match" hidden>没有匹配的错误</div></div>
      </section>`).join('');

      document.body.className = 'error-only-page';
      document.body.innerHTML = `<header class="eo-topbar"><div class="eo-topbar-inner">
        <div class="eo-brand"><div class="eo-brand-mark">参半<small>CANBAN</small></div><div class="eo-brand-copy"><strong>离线活动核销</strong><span>异常处置工作台 / ERROR DESK</span></div></div>
      </div></header><main class="eo-shell"><div class="eo-app-grid"><aside class="eo-rail" aria-label="核销场景导航">
        <div class="eo-rail-head"><div><div class="eo-rail-code">CB / OFFLINE AUDIT</div><h2>核销主工作台</h2></div><p>错误处置视图</p></div>
        <nav class="eo-rail-nav" aria-label="错误场景">${nav}</nav>
        <div class="eo-rail-foot"><span>本批次异常范围</span><strong>${projections.length} 个核销场景</strong><em>${total} 组错误待处理</em></div>
      </aside><div class="eo-workspace">
        <section class="eo-view" id="home"><div class="eo-home-cockpit">
          <header class="eo-cockpit-hero"><div class="eo-hero-copy"><span class="eo-kicker">AUDIT ERROR COMMAND</span><h1>核销错误处置总览</h1><p>当前主界面只汇总需要处理的错误。通过项、核销过程以及门店中重复出现的上游错误均不展示。</p></div><div class="eo-total-gauge"><span>待处理总数</span><strong>${total}</strong><em>组错误</em></div></header>
          <div class="eo-metric-grid"><article class="eo-metric" style="--metric-accent:#159edb"><div class="eo-metric-copy"><span>核销场景</span><small>存在错误的业务场景</small></div><strong>${projections.length}</strong></article><article class="eo-metric" style="--metric-accent:#d84b3e"><div class="eo-metric-copy"><span>错误总数</span><small>按错误清单归并后的数量</small></div><strong>${total}</strong></article><article class="eo-metric" style="--metric-accent:#b76d15"><div class="eo-metric-copy"><span>材料 / 结算错误</span><small>合同、销售、结算及道具材料</small></div><strong>${materialTotal}</strong></article><article class="eo-metric" style="--metric-accent:#173743"><div class="eo-metric-copy"><span>商品 / 门店错误</span><small>商品核销与门店现场问题</small></div><strong>${fieldTotal}</strong></article></div>
          <section class="eo-composition"><div class="eo-composition-title"><small>ERROR COMPOSITION</small><strong>错误构成</strong></div><div class="eo-composition-bar" aria-label="错误构成：材料与结算 ${materialTotal}，商品与门店 ${fieldTotal}"><i style="width:${materialPercent}%"></i><b style="width:${100 - materialPercent}%"></b></div><div class="eo-composition-legend"><span><i></i>材料 / 结算 ${materialTotal}</span><span><i></i>商品 / 门店 ${fieldTotal}</span></div></section>
          <section class="eo-queue-panel"><header class="eo-queue-head"><div><small>REJECTED SCENARIO QUEUE</small><h2>错误场景队列</h2></div><p>选择场景进入对应错误清单<br>不展示核销过程与通过记录</p></header><div class="eo-scenario-grid">${scenarioCards}</div></section>
          <div class="eo-no-match" hidden>没有匹配的错误场景</div>
        </div></section>${views}
      </div></div></main><button class="eo-back-top" id="eoBackTop" type="button">返回顶部</button>`;

      const tabs = [...document.querySelectorAll('.eo-tab')];
      const viewsById = [...document.querySelectorAll('.eo-view')];
      const openView = (id) => {
        tabs.forEach((tab) => tab.setAttribute('aria-selected', String(tab.dataset.view === id)));
        viewsById.forEach((view) => { view.hidden = view.id !== id; });
        document.body.dataset.activeView = id;
        document.querySelectorAll('[data-search]').forEach((item) => { item.hidden = false; });
        document.querySelectorAll('.eo-no-match').forEach((item) => { item.hidden = true; });
        window.scrollTo({ top: 0, behavior: 'auto' });
      };
      tabs.forEach((tab) => tab.addEventListener('click', () => openView(tab.dataset.view)));
      document.querySelectorAll('[data-open]').forEach((button) => button.addEventListener('click', () => openView(button.dataset.open)));
      document.getElementById('eoBackTop').addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
    })();
