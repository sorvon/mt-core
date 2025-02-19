from yimt.api.text_splitter import split_sentences, may_combine_paragraph, word_segment

if __name__ == "__main__":
    t1 = "How are you? Mr. White is working."
    print(word_segment(t1, lang="en"))
    print(split_sentences(t1))

    t2 = "好好学习，天天向上。运动很重要，坚持运动！"
    print(word_segment(t2, lang="zh"))
    print(split_sentences(t2, lang="zh"))

    t3 = "「日本語が話せの170理由」。タイトル：THE INFORMANT!"
    print(word_segment(t3, lang="ja"))
    print(split_sentences(t3, lang="ja"))

    t4 = '''
    The intent of this book is to introduce readers to the latest version of the
TensorFlow library. Therefore, this first chapter focuses mainly on what has
changed in the TensorFlow library since its first version, TensorFlow 1.0.
We will cover the various changes, in addition to highlighting the specific
parts for which changes are yet to be introduced. This chapter is divided
into three sections: the first discusses the internals of TensorFlow; the
second focuses on the changes that have been implemented in TensorFlow
2.0 after TensorFlow 1.0; and the final section covers TensorFlow 2.0
installation methods and basic operations.
    '''

    print(may_combine_paragraph(t4))

    t5 = "이에 따라 부산세계탁구선수권대회는 2024년 5월 24일부터 6월 2일까지 열흘 간 부산 해운대구 벡스코에서 열린다. 세계탁구선수권대회는 단일 종목으로는 가장 많은 100여개 국 2000여 명의 선수, 임원이 참여해 월드컵에 버금가는 글로벌 이벤트다. 홀수 해는 남녀 단·복식, 혼합복식 5종목이 열리고, 짝수 해에는 남녀 단체전이 진행된다."
    print(word_segment(t5, lang="ko"))
    print(split_sentences(t5, lang="ko"))
