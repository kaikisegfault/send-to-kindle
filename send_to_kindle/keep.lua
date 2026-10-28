-- send-to-kindle: keep every heading (level 2 and below) on the same page as the block after it.
--
-- Kindle doesn't always honour "page-break-after: avoid" on a heading, so a heading can end a page
-- while its text starts the next one. A block that may not be split (kindle.css: .keep) is honoured,
-- so each such heading and its first block of text go into one. Level-1 headings start chapters and
-- stay free. Headings inside wrappers (EPUBs read back, web pages) are reached too, and a title page
-- copied from another EPUB is dropped: the Kindle shows the title already.

local TEXT = {
  Para = true, Plain = true, BulletList = true, OrderedList = true, BlockQuote = true,
  Table = true, CodeBlock = true, DefinitionList = true, LineBlock = true,
}

local function keep(blocks)
  local out = pandoc.List()
  local i = 1
  while i <= #blocks do
    local b = blocks[i]
    local nxt = blocks[i + 1]
    if b.t == "Div" and b.classes:includes("titlepage") then
      i = i + 1
    elseif b.t == "Div" and b.classes:includes("keep") then
      out:insert(b)  -- kept already (an EPUB made here, read back)
      i = i + 1
    elseif b.t == "Div" then
      out:insert(pandoc.Div(keep(b.content), b.attr))  -- a new Div: older pandoc can't edit one in place
      i = i + 1
    elseif b.t == "Header" and b.level >= 2 and nxt ~= nil and TEXT[nxt.t] then
      out:insert(pandoc.Div({ b, nxt }, pandoc.Attr("", { "keep" })))
      i = i + 2
    else
      out:insert(b)
      i = i + 1
    end
  end
  return out
end

function Pandoc(doc)
  doc.blocks = keep(doc.blocks)
  return doc
end
