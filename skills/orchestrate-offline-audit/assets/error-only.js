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
      const confidenceLabel = (value) => ({ high: '高', medium: '中', low: '低' }[value] || '高');
      const normalizedConfidence = (value) => ['high', 'medium', 'low'].includes(value) ? value : 'high';
      const headerFiles = (sheet, index) => {
        const values = lines(sheet.headers?.[index] || '');
        return (values.length > 1 ? values.slice(1) : values).join('、');
      };
      const issueRows = (sheet) => (sheet.rows || []).filter((row) => row.status === 'issue');
      let errorSequence = 0;
      const textBlock = (value) => lines(value).map((line) => `<div class="eo-line">${escapeHtml(line)}</div>`).join('');
      const actionText = (row) => {
        const value = String(row.values?.[5] || '');
        const match = value.match(/要重新提交(?:什么)?：([\s\S]*)/);
        if (!match) return '补充能够直接核验该错误的清晰材料后重新提交。';
        const items = lines(match[1]).map((line) => line.replace(/^\d+\.\s*/, '')).filter(Boolean);
        return items.join('；') || '补充能够直接核验该错误的清晰材料后重新提交。';
      };
      const errorCard = ({ title, auditType, source = '', baseline = '', problem, action, extra = '', search = '', confidence = 'high' }) => `
        <article class="eo-error-card" data-audit-type="${escapeHtml(auditType)}" data-error-confidence="${normalizedConfidence(confidence)}" data-search="${escapeHtml([title, auditType, source, baseline, problem, action, search].join(' ').toLocaleLowerCase('zh-CN'))}">
          <div class="eo-error-index">${String(++errorSequence).padStart(2, '0')}</div>
          <div class="eo-error-main">
            <header class="eo-error-head"><h3>${escapeHtml(title)}</h3><div class="eo-error-badges"><span class="eo-scope">核销类型 · ${escapeHtml(auditType)}</span><span class="eo-error-confidence">置信度 · ${confidenceLabel(normalizedConfidence(confidence))}</span></div></header>
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
            reason = `结算单识别申请金额为${displayNumber(claimed)}元，转账凭证识别金额为${displayNumber(transferred)}元，两份材料相差${displayNumber(difference)}元。现有材料不能判断应以哪一个金额为准，因此金额证据链未闭合。`;
          } else if (row.heading === '收款人与日期') {
            source = transferFiles;
            baseline = excelFile;
            const storeCount = firstNumber(lineWith(row.values?.[1], 'Excel读取到'));
            reason = `销售Excel读取到${displayNumber(storeCount)}家门店；转账凭证目前只能确认金额，没有完整显示每笔收款人、对应门店和完整交易日期。因此无法确认Excel中的${displayNumber(storeCount)}家门店分别由谁收款、对应哪一笔转账以及具体转账日期。`;
          }
          const problem = textBlock(reason);
          return errorCard({ title: row.heading, auditType: '人员激励', source, baseline, problem, action: actionText(row), search: (row.values || []).join(' '), confidence: row.confidence });
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
      const displayLocalLabels = (row) => displayIssueLabels(row).filter((label) => !displayGlobalLabels.has(label));
      const displayLocalLabel = (row, label) => {
        if (!['合同门店', '门店水印错误', '门店地点待核验', '门店地点低置信度', '门店水印缺失或无法核对'].includes(label)) return label;
        if (['门店水印错误', '门店地点待核验', '门店地点低置信度'].includes(label)) return '门店地点低置信度';
        if (label !== '合同门店') return label;
        return String(row.values?.[3] || '').includes('门店不一致') ? '门店地点低置信度' : '门店水印缺失或无法核对';
      };
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
          const locationCheck = lineWith(photo, '地点核验：');
          const locationConfidence = lineWith(photo, '地点置信度：');
          const locationBasis = lineWith(photo, '地点核验依据：').replace(/^地点核验依据：/, '');
          if (hasStoreLowConfidence) {
            facts.push(`合同门店：${contractStore}`);
            facts.push(`照片水印地点：${visibleLocation}`);
            facts.push(locationCheck);
            facts.push(locationConfidence || '地点置信度：低');
            facts.push(locationBasis || '合同门店与照片水印文字不同，当前地图证据未形成高置信度同址或临近结论，转人工核验。');
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
            upstream.push({ title: '合同PDF核心字段未通过', scope: '合同材料', source: contractFile, problem: textBlock(blockingProblem.join('\n')), action: blockingAction, search: (activity.values || []).join(' '), confidence: activity.confidence });
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
            confidence: 'high',
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
            confidence: 'high',
          });
        }
        const upstreamCards = upstream.map((item) => errorCard({ ...item, auditType: '堆头/陈列' })).join('');
        const localRows = (sheet.rows || []).filter((row) => row.section === 'detail' && row.status === 'issue' && displayLocalLabels(row).length);
        const localCards = localRows.map((row) => {
          const labels = displayLocalLabels(row);
          const facts = localDisplayProblem(row, labels);
          const problem = `<div class="eo-chips">${labels.map((label) => `<span class="eo-chip">${escapeHtml(displayLocalLabel(row, label))}</span>`).join('')}</div>${facts.length ? `<div style="margin-top:8px">${textBlock(facts.join('\n'))}</div>` : ''}`;
          const source = afterPrefix(row.values?.[1], '文件：') || headerFiles(sheet, 1);
          return errorCard({ title: row.heading, auditType: '堆头/陈列', source, baseline: contractFile, problem, action: localDisplayAction(row, labels), search: (row.values || []).join(' '), confidence: row.confidence });
        }).join('');
        const html = `${upstreamCards}${localCards}`;
        return { count: upstream.length + localRows.length, upstream: upstream.length, local: localRows.length, html };
      };

      const posterView = (sheet) => {
        const rows = issueRows(sheet);
        const cards = rows.map((row) => {
          const problem = textBlock([row.values?.[1], row.values?.[4]].filter(Boolean).join('\n'));
          const source = afterPrefix(row.values?.[0], '来源：');
          return errorCard({ title: row.heading.replace(/^错误项：/, ''), auditType: '海报/展示道具', source, problem, action: actionText(row), search: (row.values || []).join(' '), confidence: row.confidence });
        }).join('');
        return { count: rows.length, upstream: rows.length, local: 0, html: cards };
      };

      const groupedIssueView = (sheet, auditType) => {
        const rows = issueRows(sheet);
        const cards = rows.map((row) => {
          const title = String(row.heading || '').replace(/^问题：/, '');
          const source = afterPrefix(row.values?.[0], '文件：');
          const problem = textBlock([row.values?.[1], row.values?.[2], row.values?.[3], row.values?.[4]].filter(Boolean).join('\n'));
          const action = afterPrefix(row.values?.[5], '处理方式：') || actionText(row);
          return errorCard({ title, auditType, source, problem, action, search: (row.values || []).join(' '), confidence: row.confidence });
        }).join('');
        return { count: rows.length, upstream: rows.length, local: 0, html: cards };
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
      const typeCount = projections.filter((item) => item.count > 0).length;
      const unifiedErrors = projections.map((item) => item.html).filter(Boolean).join('');
      const errorTypes = [...new Set(projections.map((item) => item.label))];
      const errorTypeOptions = errorTypes.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(type)}</option>`).join('');
      const errorConfidenceOptions = ['high', 'medium', 'low'].map((confidence) => `<option value="${confidence}">${confidenceLabel(confidence)}置信度</option>`).join('');

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
      const passTypeCount = passGroups.filter((group) => Number(group.item_count ?? group.items?.length ?? 0) > 0).length;
      const passScopes = passItems.reduce((counts, item) => {
        const scope = ['material', 'product', 'store', 'amount'].includes(item.scope) ? item.scope : 'material';
        counts[scope] += 1;
        return counts;
      }, { material: 0, product: 0, store: 0, amount: 0 });
      const passMaterialProduct = passScopes.material + passScopes.product;
      const passStoreAmount = passScopes.store + passScopes.amount;
      const passMaterialPercent = passTotal ? Math.round(passMaterialProduct / passTotal * 100) : 0;
      const passTypes = [...new Set(passGroups
        .map((group) => group.audit_type || group.title || '其他核销'))];
      const passCategories = [...new Set(passItems.map((item) => item.category || '核销检查'))];
      const passConfidences = ['high', 'medium', 'low'].filter((confidence) =>
        passItems.some((item) => normalizedConfidence(item.confidence) === confidence)
      );
      const passTypeOptions = passTypes.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(type)}</option>`).join('');
      const passCategoryOptions = passCategories.map((category) => `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`).join('');
      const passConfidenceOptions = passConfidences.map((confidence) => `<option value="${confidence}">${confidenceLabel(confidence)}置信度</option>`).join('');
      const sourceFiles = (item) => {
        const files = Array.isArray(item.source_files) ? item.source_files.filter(Boolean) : [];
        const totalFiles = Math.max(files.length, Number(item.source_file_count || 0));
        if (!files.length) return '';
        const chips = files.map((file) => `<span>${escapeHtml(file)}</span>`).join('');
        const remaining = totalFiles > files.length ? `<em>另有 ${totalFiles - files.length} 个来源文件</em>` : '';
        return `<div class="eo-pass-field eo-pass-source"><span>来源文件</span><div class="eo-pass-files">${chips}${remaining}</div></div>`;
      };
      let passSequence = 0;
      const passGroupHtml = passGroups.map((group, groupIndex) => {
        const items = Array.isArray(group.items) ? group.items : [];
        const categories = new Map();
        items.forEach((item) => {
          const category = item.category || '核销检查';
          if (!categories.has(category)) categories.set(category, []);
          categories.get(category).push(item);
        });
        const categoryHtml = [...categories.entries()].map(([category, categoryItems]) => {
          const cards = categoryItems.map((item) => {
            passSequence += 1;
            const confidenceValue = normalizedConfidence(item.confidence);
            const confidence = confidenceLabel(confidenceValue);
            return `<article class="eo-pass-card" data-pass-check="${escapeHtml(item.check_id || `pass-${passSequence}`)}" data-pass-category="${escapeHtml(category)}" data-pass-confidence="${confidenceValue}">
              <div class="eo-pass-index"><span>PASS</span><strong>${String(passSequence).padStart(4, '0')}</strong></div>
              <div class="eo-pass-main"><header class="eo-pass-head"><div><small>${escapeHtml(category)}</small><h3>${escapeHtml(item.title || '检查项通过')}</h3></div><span class="eo-pass-confidence">置信度 · ${confidence}</span></header>
                <div class="eo-pass-field"><span>核验对象</span><strong>${escapeHtml(item.subject || group.audit_type || '本次材料')}</strong></div>
                <div class="eo-pass-field"><span>判断依据</span><div>${textBlock(item.basis || '结构化核销结果已确认该检查项通过。')}</div></div>
                ${sourceFiles(item)}
              </div>
            </article>`;
          }).join('');
          return `<section class="eo-pass-category" data-pass-category-block="${escapeHtml(category)}"><header><span>${escapeHtml(category)}</span><em data-pass-category-visible>${categoryItems.length} 项</em></header><div class="eo-pass-stack">${cards}</div></section>`;
        }).join('');
        const auditType = group.audit_type || group.title || '其他核销';
        const categorySummary = [...categories.entries()].map(([label, categoryItems]) => `<span data-pass-breakdown-category="${escapeHtml(label)}">${escapeHtml(label)} <b data-pass-breakdown-visible>${categoryItems.length}</b></span>`).join('');
        return `<section class="eo-pass-group" data-pass-scenario="${escapeHtml(group.scenario || `group-${groupIndex}`)}" data-pass-type="${escapeHtml(auditType)}">
          <header class="eo-pass-group-head"><div class="eo-pass-group-code">${String(groupIndex + 1).padStart(2, '0')}<small>TYPE</small></div><div><small>核销类型 · ${escapeHtml(auditType)}</small><h2>${escapeHtml(group.title || group.audit_type || '正确检查项')}</h2><div class="eo-pass-breakdown">${categorySummary || '<span>暂无通过项</span>'}</div></div><div class="eo-pass-group-count"><span>当前显示</span><strong data-pass-group-visible>${items.length}</strong><em>项</em></div></header>
          ${categoryHtml || '<div class="eo-pass-empty"><strong>本类型暂无可独立确认的通过项</strong><span>这不代表未执行核验；没有充分证据的检查不会被写成通过。</span></div>'}
        </section>`;
      }).join('');

      document.body.className = 'error-only-page';
      document.body.innerHTML = `<header class="eo-topbar"><div class="eo-topbar-inner">
        <div class="eo-brand"><div class="eo-brand-mark">参半<small>CANBAN</small></div><div class="eo-brand-copy"><strong>离线活动核销</strong><span>核销判断工作台 / AUDIT DESK</span></div></div>
      </div></header><main class="eo-shell"><div class="eo-app-grid"><aside class="eo-rail" aria-label="核销结果导航">
        <div class="eo-rail-head"><div><div class="eo-rail-code">CB / OFFLINE AUDIT</div><h2>核销主工作台</h2></div><p>错误与正确检查日志</p></div>
        <nav class="eo-rail-nav" aria-label="核销结果视图"><button class="eo-tab" type="button" data-eo-view="home" aria-selected="true" aria-current="page" aria-controls="home"><span class="eo-nav-glyph">OV</span><span class="eo-nav-copy"><strong>错误总览</strong><small>本批次全部错误</small></span><em>${total}</em></button><button class="eo-tab eo-tab-pass" type="button" data-eo-view="passed" aria-selected="false" aria-controls="passed"><span class="eo-nav-glyph">OK</span><span class="eo-nav-copy"><strong>正确检查项</strong><small>按核销类型记录</small></span><em>${passTotal}</em></button></nav>
        <div class="eo-rail-foot"><span>本次核销记录</span><strong>${Math.max(typeCount, passGroups.length)} 种核销类型</strong><em>${total} 组错误 · ${passTotal} 项通过</em></div>
      </aside><div class="eo-workspace"><section class="eo-view" id="home"><div class="eo-home-cockpit">
        <header class="eo-cockpit-hero"><div class="eo-hero-copy"><span class="eo-kicker">AUDIT ERROR COMMAND</span><h1>核销错误处置总览</h1><p>当前视图直接列出全部需要处理的错误。已确认通过的子检查可切换到“正确检查项”查看，门店中重复出现的上游错误仍不重复展示。</p></div><div class="eo-total-gauge"><span>待处理总数</span><strong>${total}</strong><em>组错误</em></div></header>
        <div class="eo-metric-grid"><article class="eo-metric" style="--metric-accent:#159edb"><div class="eo-metric-copy"><span>核销类型</span><small>本批次涉及的业务类型</small></div><strong>${typeCount}</strong></article><article class="eo-metric" style="--metric-accent:#d84b3e"><div class="eo-metric-copy"><span>错误总数</span><small>按错误清单归并后的数量</small></div><strong>${total}</strong></article><article class="eo-metric" style="--metric-accent:#b76d15"><div class="eo-metric-copy"><span>材料 / 结算错误</span><small>合同、销售、结算及道具材料</small></div><strong>${materialTotal}</strong></article><article class="eo-metric" style="--metric-accent:#173743"><div class="eo-metric-copy"><span>商品 / 门店错误</span><small>商品核销与门店现场问题</small></div><strong>${fieldTotal}</strong></article></div>
        <section class="eo-composition"><div class="eo-composition-title"><small>ERROR COMPOSITION</small><strong>错误构成</strong></div><div class="eo-composition-bar" aria-label="错误构成：材料与结算 ${materialTotal}，商品与门店 ${fieldTotal}"><i style="width:${materialPercent}%"></i><b style="width:${100 - materialPercent}%"></b></div><div class="eo-composition-legend"><span><i></i>材料 / 结算 ${materialTotal}</span><span><i></i>商品 / 门店 ${fieldTotal}</span></div></section>
        <section class="eo-pass-filter-panel eo-error-filter-panel" aria-labelledby="eoErrorFilterTitle"><header class="eo-pass-filter-head"><div><small>FILTER REJECTED CHECKS</small><h2 id="eoErrorFilterTitle">筛选错误检查项</h2></div><div class="eo-pass-filter-result" aria-live="polite"><span>当前命中</span><strong id="eoErrorVisible">${total}</strong><em>/ ${total} 组</em><button type="button" data-error-reset disabled>重置筛选</button></div></header>
          <div class="eo-pass-filter-grid eo-error-filter-grid"><label><span>核销方式</span><select id="eoErrorType" aria-controls="eoErrorList"><option value="">全部核销方式</option>${errorTypeOptions}</select></label><label><span>置信度</span><select id="eoErrorConfidence" aria-controls="eoErrorList"><option value="">全部置信度</option>${errorConfidenceOptions}</select></label><label class="eo-error-search"><span>关键词</span><input id="eoErrorKeyword" type="search" placeholder="搜索错误项、文件、原因或处理方式" autocomplete="off" aria-controls="eoErrorList"></label></div>
          <div class="eo-pass-facet-hint eo-facet-summary-only"><em id="eoErrorFacetSummary" aria-live="polite"></em></div>
        </section>
        <section class="eo-queue-panel"><header class="eo-queue-head"><div><small>REJECTED ERROR RESULTS</small><h2>核销错误结果</h2></div><p>全部错误直接向下排列<br>每项标明对应核销类型</p></header><div class="eo-scenario-grid eo-stack" id="eoErrorList">${unifiedErrors || '<div class="eo-empty">没有发现核销错误</div>'}</div></section>
        <div class="eo-pass-filter-empty eo-error-filter-empty" id="eoErrorFilterEmpty" hidden><strong>没有符合条件的错误检查项</strong><span>可调整核销方式、置信度或关键词后重试。</span><button type="button" data-error-reset>清除全部筛选</button></div>
      </div></section><section class="eo-view" id="passed" hidden><div class="eo-home-cockpit eo-pass-cockpit">
        <header class="eo-cockpit-hero eo-pass-hero"><div class="eo-hero-copy"><span class="eo-kicker">VERIFIED CHECK LEDGER</span><h1>正确检查项日志</h1><p>按核销类型记录已有充分证据、被独立确认通过的子检查，保留核验对象、判断依据、来源文件与置信度，便于人工回看。</p></div><div class="eo-total-gauge"><span>通过检查项</span><strong>${passTotal}</strong><em>项记录</em></div></header>
        <div class="eo-metric-grid"><article class="eo-metric" style="--metric-accent:#159edb"><div class="eo-metric-copy"><span>核销类型</span><small>形成通过日志的业务类型</small></div><strong>${passTypeCount}</strong></article><article class="eo-metric" style="--metric-accent:#18845e"><div class="eo-metric-copy"><span>通过检查项</span><small>逐个独立子检查计数</small></div><strong>${passTotal}</strong></article><article class="eo-metric" style="--metric-accent:#288d86"><div class="eo-metric-copy"><span>材料 / 商品</span><small>合同、票据、销售与商品检查</small></div><strong>${passMaterialProduct}</strong></article><article class="eo-metric" style="--metric-accent:#173743"><div class="eo-metric-copy"><span>门店 / 金额</span><small>现场、地点、日期与金额规则</small></div><strong>${passStoreAmount}</strong></article></div>
        <section class="eo-composition eo-pass-composition"><div class="eo-composition-title"><small>PASS COMPOSITION</small><strong>通过项构成</strong></div><div class="eo-composition-bar" aria-label="通过项构成：材料与商品 ${passMaterialProduct}，门店与金额 ${passStoreAmount}"><i style="width:${passMaterialPercent}%"></i><b style="width:${100 - passMaterialPercent}%"></b></div><div class="eo-composition-legend"><span><i></i>材料 / 商品 ${passMaterialProduct}</span><span><i></i>门店 / 金额 ${passStoreAmount}</span></div></section>
        <section class="eo-pass-filter-panel" aria-labelledby="eoPassFilterTitle"><header class="eo-pass-filter-head"><div><small>FILTER VERIFIED CHECKS</small><h2 id="eoPassFilterTitle">筛选正确检查项</h2></div><div class="eo-pass-filter-result" aria-live="polite"><span>当前命中</span><strong id="eoPassVisible">${passTotal}</strong><em>/ ${passTotal} 项</em><button type="button" data-pass-reset disabled>重置筛选</button></div></header>
          <div class="eo-pass-filter-grid"><label><span>核销方式</span><select id="eoPassType" aria-controls="eoPassList"><option value="">全部核销方式</option>${passTypeOptions}</select></label><label><span>检查分类</span><select id="eoPassCategory" aria-controls="eoPassList"><option value="">全部检查分类</option>${passCategoryOptions}</select></label><label><span>置信度</span><select id="eoPassConfidence" aria-controls="eoPassList"><option value="">全部置信度</option>${passConfidenceOptions}</select></label><label class="eo-pass-search"><span>关键词</span><input id="eoPassKeyword" type="search" placeholder="搜索检查项、对象、依据或文件" autocomplete="off" aria-controls="eoPassList"></label></div>
          <div class="eo-pass-facet-hint eo-facet-summary-only"><em id="eoPassFacetSummary" aria-live="polite"></em></div>
        </section>
        <section class="eo-pass-ledger"><header class="eo-queue-head"><div><small>VERIFIED PASS RESULTS</small><h2>按核销类型归档</h2></div><p>${passGroups.length} 种核销类型<br>${passTotal} 项可回查判断</p></header><div class="eo-pass-groups" id="eoPassList">${passGroupHtml || '<div class="eo-pass-empty"><strong>暂无正确检查项日志</strong><span>只有被结构化结果明确判定通过的子检查才会出现在这里。</span></div>'}</div></section>
        <div class="eo-pass-filter-empty" id="eoPassFilterEmpty" hidden><strong>没有符合条件的正确检查项</strong><span>可调整核销方式、检查分类、置信度或关键词后重试。</span><button type="button" data-pass-reset>清除全部筛选</button></div>
      </div></section></div></div></main><button class="eo-back-top" id="eoBackTop" type="button">返回顶部</button>`;

      document.getElementById('eoBackTop').addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
      const normalizeFilterText = (value) => String(value || '').trim().toLocaleLowerCase('zh-CN');
      const renderFacetOptions = ({ select, values, allLabel, label, counts, selected }) => {
        const availableValues = values.filter((value) => counts.has(value));
        if (selected && !availableValues.includes(selected)) availableValues.unshift(selected);
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
        select.replaceChildren(allOption, ...options);
        select.value = selected;
        select.disabled = availableValues.length === 0;
        return counts.size;
      };

      const errorTypeFilter = document.getElementById('eoErrorType');
      const errorConfidenceFilter = document.getElementById('eoErrorConfidence');
      const errorKeywordFilter = document.getElementById('eoErrorKeyword');
      const errorFilterPanel = document.querySelector('.eo-error-filter-panel');
      const errorVisible = document.getElementById('eoErrorVisible');
      const errorFilterEmpty = document.getElementById('eoErrorFilterEmpty');
      const errorFacetSummary = document.getElementById('eoErrorFacetSummary');
      const errorFilterRecords = [...document.querySelectorAll('#eoErrorList .eo-error-card')].map((card) => ({
        card,
        type: card.dataset.auditType || '其他费用',
        confidence: card.dataset.errorConfidence || 'high',
        search: normalizeFilterText(card.textContent),
      }));
      const errorGlobalTypeCounts = errorFilterRecords.reduce((counts, record) => {
        counts.set(record.type, (counts.get(record.type) || 0) + 1);
        return counts;
      }, new Map(errorTypes.map((type) => [type, 0])));
      const errorConfidences = ['high', 'medium', 'low'].filter((confidence) =>
        errorFilterRecords.some((record) => record.confidence === confidence)
      );
      const readErrorFilterState = () => ({
        type: errorTypeFilter.value,
        confidence: errorConfidenceFilter.value,
        keyword: normalizeFilterText(errorKeywordFilter.value),
      });
      const errorRecordMatches = (record, state, excludedFacet = '') => (
        (excludedFacet === 'type' || !state.type || record.type === state.type)
        && (excludedFacet === 'confidence' || !state.confidence || record.confidence === state.confidence)
        && (!state.keyword || record.search.includes(state.keyword))
      );
      const errorConfidenceCounts = (state) => errorFilterRecords.reduce((counts, record) => {
        if (!errorRecordMatches(record, state, 'confidence')) return counts;
        counts.set(record.confidence, (counts.get(record.confidence) || 0) + 1);
        return counts;
      }, new Map());
      const refreshErrorFacets = () => {
        const state = readErrorFilterState();
        renderFacetOptions({
          select: errorTypeFilter,
          values: errorTypes,
          allLabel: '全部核销方式',
          label: (value) => value,
          counts: errorGlobalTypeCounts,
          selected: state.type,
        });
        const confidenceCount = renderFacetOptions({
          select: errorConfidenceFilter,
          values: errorConfidences,
          allLabel: '全部置信度',
          label: (value) => `${confidenceLabel(value)}置信度`,
          counts: errorConfidenceCounts(state),
          selected: state.confidence,
        });
        errorFacetSummary.textContent = `${errorGlobalTypeCounts.size} 种核销方式 · ${confidenceCount} 档可选置信度`;
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
        const filtering = Boolean(state.type || state.confidence || state.keyword);
        errorVisible.textContent = String(visibleTotal);
        errorFilterPanel.classList.toggle('is-filtering', filtering);
        errorFilterEmpty.hidden = visibleTotal !== 0 || total === 0;
        document.querySelectorAll('[data-error-reset]').forEach((button) => {
          if (!button.closest('.eo-error-filter-empty')) button.disabled = !filtering;
        });
      };
      const normalizeErrorDependentsForType = () => {
        const state = readErrorFilterState();
        const counts = errorConfidenceCounts(state);
        if (state.confidence && !counts.has(state.confidence)) errorConfidenceFilter.value = '';
      };
      const resetErrorFilters = () => {
        errorTypeFilter.value = '';
        errorConfidenceFilter.value = '';
        errorKeywordFilter.value = '';
        applyErrorFilters();
      };
      errorTypeFilter.addEventListener('change', () => {
        normalizeErrorDependentsForType();
        applyErrorFilters();
      });
      errorConfidenceFilter.addEventListener('change', applyErrorFilters);
      errorKeywordFilter.addEventListener('input', applyErrorFilters);
      document.querySelectorAll('[data-error-reset]').forEach((button) => button.addEventListener('click', resetErrorFilters));
      applyErrorFilters();

      const passTypeFilter = document.getElementById('eoPassType');
      const passCategoryFilter = document.getElementById('eoPassCategory');
      const passConfidenceFilter = document.getElementById('eoPassConfidence');
      const passKeywordFilter = document.getElementById('eoPassKeyword');
      const passFilterPanel = document.querySelector('#passed .eo-pass-filter-panel');
      const passVisible = document.getElementById('eoPassVisible');
      const passFilterEmpty = document.getElementById('eoPassFilterEmpty');
      const passFacetSummary = document.getElementById('eoPassFacetSummary');
      const passFilterRecords = [...document.querySelectorAll('#eoPassList .eo-pass-card')].map((card) => ({
        card,
        type: card.closest('.eo-pass-group')?.dataset.passType || '其他核销',
        category: card.dataset.passCategory || '核销检查',
        confidence: card.dataset.passConfidence || 'high',
        search: normalizeFilterText(card.textContent),
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
        confidence: {
          select: passConfidenceFilter,
          values: passConfidences,
          allLabel: '全部置信度',
          label: (value) => `${confidenceLabel(value)}置信度`,
        },
      };
      const readPassFilterState = () => ({
        type: passTypeFilter.value,
        category: passCategoryFilter.value,
        confidence: passConfidenceFilter.value,
        keyword: normalizeFilterText(passKeywordFilter.value),
      });
      const passRecordMatches = (record, state, excludedFacet = '') => (
        (excludedFacet === 'type' || !state.type || record.type === state.type)
        && (excludedFacet === 'category' || !state.category || record.category === state.category)
        && (excludedFacet === 'confidence' || !state.confidence || record.confidence === state.confidence)
        && (!state.keyword || record.search.includes(state.keyword))
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
        });
      };
      const refreshPassFacets = () => {
        const state = readPassFilterState();
        const available = {
          type: renderPassFacet('type', state),
          category: renderPassFacet('category', state),
          confidence: renderPassFacet('confidence', state),
        };
        passFacetSummary.textContent = `${available.type} 种核销方式 · ${available.category} 类可选检查 · ${available.confidence} 档可选置信度`;
        return readPassFilterState();
      };
      const applyPassFilters = () => {
        const state = refreshPassFacets();
        let visibleTotal = 0;
        document.querySelectorAll('#eoPassList .eo-pass-group').forEach((group) => {
          const typeMatches = !state.type || group.dataset.passType === state.type;
          let groupVisible = 0;
          const categoryCounts = new Map();
          group.querySelectorAll('.eo-pass-category').forEach((categoryBlock) => {
            let categoryVisible = 0;
            categoryBlock.querySelectorAll('.eo-pass-card').forEach((card) => {
              const categoryMatches = !state.category || card.dataset.passCategory === state.category;
              const confidenceMatches = !state.confidence || card.dataset.passConfidence === state.confidence;
              const keywordMatches = !state.keyword || normalizeFilterText(card.textContent).includes(state.keyword);
              const matches = typeMatches && categoryMatches && confidenceMatches && keywordMatches;
              card.hidden = !matches;
              if (matches) categoryVisible += 1;
            });
            categoryBlock.hidden = categoryVisible === 0;
            const categoryName = categoryBlock.dataset.passCategoryBlock || '核销检查';
            categoryCounts.set(categoryName, categoryVisible);
            const categoryVisibleLabel = categoryBlock.querySelector('[data-pass-category-visible]');
            if (categoryVisibleLabel) categoryVisibleLabel.textContent = `${categoryVisible} 项`;
            groupVisible += categoryVisible;
          });
          group.querySelectorAll('[data-pass-breakdown-category]').forEach((badge) => {
            const categoryName = badge.dataset.passBreakdownCategory || '';
            const categoryCount = categoryCounts.get(categoryName) || 0;
            badge.hidden = categoryCount === 0;
            const badgeCount = badge.querySelector('[data-pass-breakdown-visible]');
            if (badgeCount) badgeCount.textContent = String(categoryCount);
          });
          group.hidden = groupVisible === 0;
          const groupVisibleLabel = group.querySelector('[data-pass-group-visible]');
          if (groupVisibleLabel) groupVisibleLabel.textContent = String(groupVisible);
          visibleTotal += groupVisible;
        });
        const filtering = Boolean(state.type || state.category || state.confidence || state.keyword);
        passVisible.textContent = String(visibleTotal);
        passFilterPanel.classList.toggle('is-filtering', filtering);
        passFilterEmpty.hidden = visibleTotal !== 0 || passTotal === 0;
        document.querySelectorAll('[data-pass-reset]').forEach((button) => {
          if (!button.closest('.eo-pass-filter-empty')) button.disabled = !filtering;
        });
      };
      const resetPassFilters = () => {
        passTypeFilter.value = '';
        passCategoryFilter.value = '';
        passConfidenceFilter.value = '';
        passKeywordFilter.value = '';
        applyPassFilters();
      };
      const normalizePassDependentsForType = () => {
        let state = readPassFilterState();
        const categoryCounts = passFacetCounts('category', state);
        if (state.category && !categoryCounts.has(state.category)) passCategoryFilter.value = '';
        state = readPassFilterState();
        const confidenceCounts = passFacetCounts('confidence', state);
        if (state.confidence && !confidenceCounts.has(state.confidence)) passConfidenceFilter.value = '';
      };
      passTypeFilter.addEventListener('change', () => {
        normalizePassDependentsForType();
        applyPassFilters();
      });
      [passCategoryFilter, passConfidenceFilter].forEach((control) => control.addEventListener('change', applyPassFilters));
      passKeywordFilter.addEventListener('input', applyPassFilters);
      document.querySelectorAll('[data-pass-reset]').forEach((button) => button.addEventListener('click', resetPassFilters));
      applyPassFilters();
      const resultTabs = [...document.querySelectorAll('[data-eo-view]')];
      const openResultView = (viewId) => {
        resultTabs.forEach((tab) => {
          const selected = tab.dataset.eoView === viewId;
          tab.setAttribute('aria-selected', String(selected));
          if (selected) tab.setAttribute('aria-current', 'page');
          else tab.removeAttribute('aria-current');
        });
        document.querySelectorAll('.eo-workspace > .eo-view').forEach((view) => {
          view.hidden = view.id !== viewId;
        });
        window.scrollTo({ top: 0, behavior: 'auto' });
      };
      resultTabs.forEach((tab) => tab.addEventListener('click', () => openResultView(tab.dataset.eoView)));
    })();
