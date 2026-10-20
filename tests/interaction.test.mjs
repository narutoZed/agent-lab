import test from 'node:test';
import assert from 'node:assert/strict';
import { recommendedOptions, answerRecommended, workbuddyDecision, codexDecision } from '../scripts/interaction_policy.mjs';
const policy={questions:'recommended',permissions:'approve'};

test('only explicit recommendations are selected',()=>{
  assert.deepEqual(recommendedOptions({options:[{label:'A'},{label:'B (Recommended)'}]}),['B (Recommended)']);
  assert.deepEqual(recommendedOptions({options:[{label:'选项甲（推荐）'},{label:'选项乙'}]}),['选项甲（推荐）']);
  assert.equal(recommendedOptions({options:[{label:'not recommended'}]}),null);
  assert.equal(recommendedOptions({options:[{label:'A'},{label:'B'}]}),null);
  assert.equal(recommendedOptions({isSecret:true,options:[{label:'A (Recommended)'}]}),null);
});
test('multi-select and native answer envelopes differ correctly',()=>{
  const questions=[{id:'q0',multiSelect:true,options:[{label:'A',recommended:true},{label:'B',recommended:true}]}];
  assert.deepEqual(answerRecommended(questions),{q0:{answers:['A','B']}});
  assert.deepEqual(answerRecommended(questions,'workbuddy'),{q0:['A','B']});
  assert.equal(answerRecommended([{id:'q',options:null}]),null);
});
test('WorkBuddy tool approvals choose one-time permission',()=>{
  const p={id:'call',category:'tool',options:[{optionId:'deny',kind:'reject_once'},{optionId:'always',kind:'allow_always'},{optionId:'once',kind:'allow_once'}]};
  assert.deepEqual(workbuddyDecision(p,policy),{kind:'permission',response:{optionId:'once'}});
  assert.equal(workbuddyDecision({...p,category:'mcpConnection'},policy),null);
  assert.equal(workbuddyDecision(p,{permissions:'manual'}),null);
});
test('WorkBuddy questions submit the actual recommended label',()=>{
  const p={id:'ask',category:'question',questions:[{id:'q_0',options:[{label:'甲（推荐）'},{label:'乙'}]}]};
  assert.deepEqual(workbuddyDecision(p,policy).response,{optionId:'allow',answers:{q_0:'甲（推荐）'}});
});
test('Codex approvals are scoped to this thread and requested permissions',()=>{
  const p={threadId:'owned',permissions:{network:{enabled:true}}};
  assert.equal(codexDecision('item/commandExecution/requestApproval',p,policy,'other'),null);
  assert.deepEqual(codexDecision('item/commandExecution/requestApproval',p,policy,'owned').response,{decision:'accept'});
  assert.deepEqual(codexDecision('item/permissions/requestApproval',p,policy,'owned').response,{permissions:p.permissions,scope:'turn'});
  assert.equal(codexDecision('unknown/requestApproval',p,policy,'owned'),null);
  const app={threadId:'owned',questions:[{id:'p',options:[{label:'Accept'},{label:'Decline'},{label:'Cancel'}]}]};
  assert.equal(codexDecision('item/tool/requestUserInput',app,policy,'owned').kind,'permission');
});
