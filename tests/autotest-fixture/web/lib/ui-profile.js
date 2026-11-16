// What only this project knows about comparing its screens with its design package (read by autotest/uicheck).
// The made-up product "Kho sách": one list of books, read from the page itself; the design takes the book of a
// detail dialog by position (the first of data.books).
const MAP = require('./ui-profile.map.json');

const TOP_BAR = 56;   // px: the design draws a placeholder bar, the product its own bar

const sameBook = (b, book) => b.code === book || b.name === book;

// Only for the fixture's own tests: this product's data never changes by itself, so a test asks for it. opts.changing = n
// makes the first n reads of one check each differ from the next, as the data of a real product does while other
// tests run; `reads` counts every read.
const readsOf = new WeakMap();

module.exports = {
  reads: 0,

  // design screens that show no data
  anonymous: ['help'],

  /** The list of books as the page shows it, in the shape of the package's data. */
  async readData({ page, read, opts }) {
    const view = await read.view(page);
    const list = read.rows(view, MAP.books, 'sách');
    if (!list) throw new Error('Không đọc được bảng sách trên màn Kho sách');
    module.exports.reads++;
    const nth = (readsOf.get(opts) || 0) + 1;
    readsOf.set(opts, nth);
    return { books: list.rows, ...(nth <= (opts.changing || 0) ? { readNo: nth } : {}) };
  },

  /** opts.book: code or name of the book the screen is about; the design draws the first book of the data. */
  arrange({ data, opts }) {
    const i = opts.book ? data.books.findIndex(b => sameBook(b, opts.book)) : -1;
    if (i > 0) data.books.unshift(data.books.splice(i, 1)[0]);
    return data;
  },

  /** A detail dialog names its book: the design would draw the first book of the data. */
  sameObject({ data, code, opts, webText }) {
    if (opts.book && !data.books.some(b => sameBook(b, opts.book))) return `sách "${opts.book}" không có trong dữ liệu đọc được từ web`;
    if (code !== 'BK-3') return null;
    const first = data.books[0];
    if (!first) return 'dữ liệu đọc từ web không có đầu sách nào cho hộp thoại chi tiết';
    if (!webText.text.includes(first.name)) return `design sẽ vẽ sách "${first.name}", màn web không có (web đang ở sách khác)`;
    return null;
  },

  /** The values a test typed in the "Thêm sách" dialog go to the design as data.demo.newBook (opts.typed). */
  designInput({ state, data, opts }) {
    if (!data) return { ...state.input };
    return { ...state.input, data: { ...data, demo: { newBook: opts.typed || { code: '', name: '', author: '' } } } };
  },

  /** On a whole screen the top bar is not compared: the design only draws a bar that holds its place. */
  region({ scope, viewport }) {
    return scope === 'page' ? { drop: { x: 0, y: 0, w: viewport.width, h: TOP_BAR } } : null;
  },

  partNote({ scope }) {
    return scope === 'page' ? `So phần nội dung dưới thanh đầu trang; thanh đầu trang (cao ${TOP_BAR} px) không so vì design chỉ vẽ một thanh giữ chỗ.` : null;
  },

  notWeb(diff, ctx) {
    if (/^Chữ "Cập nhật lúc /.test(diff.element)) return 'Gói v0.1: chân bảng của design chỉ có dòng "Tổng số …", chưa có dòng "Cập nhật lúc …"';
    if (diff.what === 'vòng focus' && ctx.webNode && ctx.webNode.state && ctx.webNode.state.focused)
      return 'Cách đo: web đang đặt con trỏ ở ô này; trang design mở từ ngoài không đặt con trỏ vào ô nào';
    return null;
  },

  packageLimits: [
    'Thanh đầu trang: design chỉ vẽ một thanh giữ chỗ; web có tên sản phẩm và tên người dùng.',
    'Chân bảng: design chỉ có dòng "Tổng số …"; web có thêm dòng "Cập nhật lúc …".',
    'Ô đang nhập của hộp thoại: web có vòng sáng quanh ô đang đặt con trỏ, design không.',
  ],
};
