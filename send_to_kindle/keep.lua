-- send-to-kindle: keep every heading (level 2 and below) on the same page as the block after it.
--
-- Kindle doesn't always honour "page-break-after: avoid" on a heading, so a heading can end a page
-- while its text starts the next one. A block that may not be split (kindle.css: div.keep) is honoured,
-- so each such heading and its first block go into one. Level-1 headings start chapters and stay free.

function Pandoc(doc)
  local out = pandoc.List()
  local blocks = doc.blocks
  local i = 1
  while i <= #blocks do
    local b = blocks[i]
    local nxt = blocks[i + 1]
    if b.t == "Header" and b.level >= 2 and nxt ~= nil and nxt.t ~= "Header" then
      out:insert(pandoc.Div({ b, nxt }, pandoc.Attr("", { "keep" })))
      i = i + 2
    else
      out:insert(b)
      i = i + 1
    end
  end
  doc.blocks = out
  return doc
end
