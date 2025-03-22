from enum import Enum


class EditType(Enum):
    SKIP = 1
    ADD = 2
    REPLACE = 3
    DEL = 4


class DistanceInfo:
    def __init__(self, v, edit_type):
        if isinstance(v, DistanceInfo):
            self.v = v.v
        else:
            self.v = v

        self.edit_type = edit_type

    def __add__(self, other):
        # 只能和数字加，不需要跟其它dist_info加
        return DistanceInfo(self.v + int(other), self.edit_type)

    def __str__(self):
        return f"v: {self.v}, edit_type: {self.edit_type}"

    @staticmethod
    def compare_value(dist_info):
        return dist_info.v


class MinDistance:
    def __init__(self):
        self.mem = None
        self.str1 = None
        self.str2 = None

    def min_distance(self, str1, str2):
        # 用于获取编辑路径，下面计算的是str1经过怎样的操作才能变成str2
        self.str1 = str1
        self.str2 = str2

        self.mem = [[-1] * len(str2) for _ in range(len(str1))]
        return self.dp(str1, len(str1) - 1, str2, len(str2) - 1).v

    def dp(self, str1, i, str2, j):
        if i == - 1:
            return DistanceInfo(j + 1, EditType.ADD)

        if j == -1:
            return DistanceInfo(i + 1, EditType.DEL)

        if self.mem[i][j] != -1:
            return self.mem[i][j]

        if str1[i] == str2[j]:
            self.mem[i][j] = DistanceInfo(self.dp(str1, i - 1, str2, j - 1), EditType.SKIP)
            return self.mem[i][j]

        self.mem[i][j] = min(DistanceInfo(self.dp(str1, i - 1, str2, j), EditType.DEL),  # str1删
                             DistanceInfo(self.dp(str1, i, str2, j - 1), EditType.ADD),  # str1插
                             DistanceInfo(self.dp(str1, i - 1, str2, j - 1), EditType.REPLACE),  # str1替换
                             key=DistanceInfo.compare_value) + 1
        return self.mem[i][j]

    def get_edit_trace(self, need_skip=True):
        edit_trace = []
        str1 = self.str1
        str2 = self.str2
        i = len(str1) - 1
        j = len(str2) - 1

        while True:
            if i == -1:
                while j >= 0:
                    edit_trace.append((str2[j], EditType.ADD))
                    j -= 1
                break

            if j == -1:
                while i >= 0:
                    edit_trace.append((str1[i], EditType.DEL))
                    i -= 1
                break

            dist_info = self.mem[i][j]
            _, edit_type = dist_info.v, dist_info.edit_type

            if edit_type == EditType.SKIP:
                edit_char = str1[i]
                i -= 1
                j -= 1

                if not need_skip:
                    continue

            elif edit_type == EditType.ADD:
                edit_char = str2[j]
                j -= 1
            elif edit_type == EditType.REPLACE:
                edit_char = str2[j]
                i -= 1
                j -= 1
            elif edit_type == EditType.DEL:
                edit_char = str1[i]
                i -= 1
            else:
                raise ValueError(f'illegal edit_type, dist_info: {dist_info}')

            edit_trace.append((edit_char, edit_type))
        return edit_trace


if __name__ == '__main__':
    minD = MinDistance()
    # print(minD.min_distance('rad', 'apple'))
    # print(minD.get_edit_trace())

    print(minD.min_distance('05. 1.3.1 环境搭建', '05．1.3.1环境搭建'))

    from pprint import pprint
    pprint(minD.get_edit_trace())
