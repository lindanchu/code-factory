// The profile of the edge tests: every hook of the contract, each doing what the test asks for in opts, and noting
// how it was called (calls) so a test can look at it.
module.exports = {
  calls: [],

  anonymous: ['anon'],

  me({ opts }) {
    module.exports.calls.push('me');
    return opts.who || 'từ trang';
  },

  async readData({ state, me, scope }) {
    module.exports.calls.push(`readData:${scope}:${me}`);
    if (state.screen === 'nodata') return undefined;   // a profile that forgets to return its data
    return { who: 'Lan Phạm', items: ['Sửa', 'Khoá', 'Xoá'] };
  },

  arrange({ data, opts, me, note, webText }) {
    if (opts.noteIt) note(`Cách đo: ghi chú của profile cho ${me}, thấy "${webText.head}"`);
    return data;
  },

  sameObject({ opts }) {
    return opts.other ? 'không so được: đã nói sẵn là không so được' : null;
  },

  region({ opts }) {
    return opts.region || null;
  },

  notWeb(diff, ctx) {
    return ctx.opts.dropAll ? 'bỏ hết để thử' : null;
  },
};
