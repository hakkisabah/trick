"""Function words, dropped from a query before it is scored.

This file is data, not prose: the repository is English-only, and a Turkish
stopword list cannot be written any other way. The language gate exempts this
one path for that reason (see ``scripts/language_gate.py``).

A question is mostly function words. "i5 en cok problem yasadigimiz yer" is four
content words and three that occur in every turn; searching the function words
too returned three rows that shared only "en" and "cok", which reads as a wrong
answer. English is here for the same reason a mixed-language chat exists at all.
"""

from __future__ import annotations

STOPWORDS = frozenset(
    """
    acaba ama ancak artık aslında az bana bazı belki ben beni benim beş bile bir
    biraz birçok bize biz bu buna bunda bundan bunu bunun burada çok çünkü da daha
    de defa değil diğer diye dolayı edilecek eden ederek edilen edilmesi eğer
    elbette en fakat falan farklı gibi göre hala hangi hatta hem hemen hep hepsi
    her herhangi herkes hiç hiçbir için içinde iki ile ilgili ise işte kadar kendi
    kez ki kim kimi kimse la mı mi mu mü nasıl ne neden nedenle nerede nereye niçin
    niye o olan olarak oldu olduğu olduğunu oldukça olmadı olmadığı olmak olması
    olmayan olmaz olsa olsun on ona ondan onlar onları onun orada oysa öyle önce
    özellikle pek rağmen sadece sanki sen seni senin siz size son sonra şey şeyi
    şimdi şu şuna şunda şundan şunu şunun tüm tümü üç üzere var vardı ve veya ya
    yani yapılan yapacak yapmak yaptı yaptığı yaptığını yaptıkları yerine yine
    yoksa yukarıda zaten
    about above after again against all also and any are because been before
    being below between both but can cannot could did does doing down during each
    few for from further had has have having her here hers him his how into its
    itself just me more most must my no nor not now off once only other our out
    over own same she should some such than that the their them then there these
    they this those through under until very was were what when where which while
    who whom why will with would you your
    """.split()
)

__all__ = ["STOPWORDS"]
