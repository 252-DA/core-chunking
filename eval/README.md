# Bộ câu hỏi đo truy xuất theo trang

`retrieval_questions.jsonl` (111 câu) dùng cho `scripts/eval_retrieval_pages.py`.
Mỗi dòng: `id`, `type` (lookup / definition / relational / global), `question`
(tiếng Việt), `reference` (đáp án ngắn), `evidence` (danh sách
`{"document", "page"}`), `answerable`, `origin`.

## Nguồn

- 27 câu `origin: "seed"` lấy từ `ai-tutor/eval/questions.seed.jsonl`, chỉ những
  câu có trang đáp án. File seed không bị sửa: nó còn được `chat_eval` dùng.
- 84 câu `origin: "2026-10-28"` viết thêm từ 10 tài liệu CO3137 (slide các chương
  Ch1, Ch2, LSH, Ch6, MapReduce, Spark, Lambda, giảm chiều; hai bài báo Delta Lake
  và Lakehouse). Câu hỏi diễn đạt lại, không chép nguyên câu trong slide.

## Cách gán trang đáp án

Gộp ứng viên (pooling) như TREC: với mỗi câu, lấy mọi trang nằm trong top-6
chunk của bất kỳ biến thể nào trong 5 biến thể của script (v1/v2 × flow/slides,
v2 không gộp trang), có OCR. Tổng 1.227 cặp (câu, trang) được đọc và đánh dấu.

Một trang được tính là đáp án khi nó trả lời trực tiếp câu hỏi, hoặc một ý thực
chất của câu nhiều ý. Không tính: trang chỉ nhắc chủ đề, slide chỉ có tiêu đề,
một dòng tóm tắt pipeline. Bản slide lặp lại để làm hiệu ứng (cùng đáp án) thì
tính.

Kết quả: thêm 78 trang cho 40 câu, không bỏ trang nào. Hai trang seed ghi sai đã
sửa: def-07 (đáp án ở LSH tr.27) và rel-02 (MapReduce tr.66).

## Giới hạn

- Một người gán (Claude), chưa có người thứ hai đối chiếu.
- Trang ngoài top-6 của mọi biến thể chưa được soát. MRR@10 có thể hơi thấp hơn
  thật ở hạng 7–10.
- Chữ trong hình chỉ có khi Docling OCR đọc được.
- Tên file và số trang theo bản tải từ MinIO ngày 2026-10-27; đổi file thì phải
  gán lại.
